"""Required-model FableDan adapter. No GUI or capture dependency."""
from __future__ import annotations

from collections import Counter
from functools import lru_cache
import hashlib
from pathlib import Path
from time import perf_counter
from typing import Any

import numpy as np

from guance.contracts import AdviceCandidate, AdviceResult, GameObservation, PublicAction
from .fabledan_vendor.cards import rank_of
from .fabledan_vendor.combos import (
    PASS, SINGLE, PAIR, TRIPLE, FULL, STRAIGHT, PLATE, TUBE, BOMB, SFLUSH,
    ROCKET, Move, gen_moves, beats,
)
from .fabledan_vendor.encode import encode_decision, FEAT_DIM, VOCAB
from .fabledan_vendor.model_np import NumpyModel

TYPE_MAP = {1:SINGLE,2:PAIR,3:TRIPLE,4:STRAIGHT,5:FULL,6:PLATE,7:TUBE,8:BOMB,9:SFLUSH,10:BOMB}
TYPE_LABELS = {PASS:'不出',SINGLE:'单张',PAIR:'对子',TRIPLE:'三张',FULL:'三带二',
               STRAIGHT:'顺子',PLATE:'三连对',TUBE:'钢板',BOMB:'炸弹',SFLUSH:'同花顺',ROCKET:'四王炸'}
SUIT_TO_FABLE = {1:2,2:0,3:3,4:1}
FABLE_TO_SUIT = {v:k for k,v in SUIT_TO_FABLE.items()}


class ModelError(ValueError):
    """An exact, required-model decision could not be produced."""


def to_fable(code: int) -> int:
    suit, rank, copy = (code >> 4) & 15, code & 15, code >> 8
    if copy not in (0,1):
        raise ModelError('实体牌编号无效')
    if suit == 0 and rank in (14,15):
        return 52 + rank - 14 + 54 * copy
    if suit not in SUIT_TO_FABLE or rank not in range(1,14):
        raise ModelError('牌面编号无效')
    return (rank-1)*4 + SUIT_TO_FABLE[suit] + 54*copy


def from_fable(value: int) -> int:
    if not 0 <= value < 108:
        raise ModelError('模型返回了无效实体牌')
    copy, base = divmod(value,54)
    if base >= 52:
        return (14+base-52) | (copy<<8)
    rank, suit = divmod(base,4)
    return (FABLE_TO_SUIT[suit]<<4) | (rank+1) | (copy<<8)


@lru_cache(maxsize=1024)
def _full_moves(cards: tuple[int,...], level: int) -> tuple[Move,...]:
    wanted = Counter(cards)
    return tuple(m for m in gen_moves(list(cards),level,None)
                 if m.type != PASS and Counter(m.cards)==wanted)


def _semantic_key(move: Move) -> tuple[Any,...]:
    return move.type, move.key, tuple(sorted(move.claim_ranks))


def _resolve_action(action: PublicAction, level: int, previous: Move | None) -> Move:
    cards = tuple(sorted(to_fable(c) for c in action.cards))
    candidates = list(_full_moves(cards,level))
    expected = TYPE_MAP.get(action.combo_type)
    if expected is not None:
        candidates = [m for m in candidates if m.type==expected]
    if previous is not None:
        candidates = [m for m in candidates if beats(m,previous,level)]
    unique = {_semantic_key(m):m for m in candidates}
    if len(unique)!=1:
        raise ModelError(f'第 {action.index} 个公开动作的配牌或牌型语义不能唯一确定')
    return next(iter(unique.values()))


def build_model_observation(observation: GameObservation) -> dict[str,Any]:
    if observation.current_seat != observation.my_seat:
        raise ModelError('当前不是自己的回合')
    if not 1 <= observation.level <= 13 or not 0 <= observation.my_seat < 4:
        raise ModelError('级牌或座位无效')
    if observation.partner_seat != (observation.my_seat+2)%4:
        raise ModelError('队友关系无效')
    level = observation.level-1
    hand = [to_fable(c) for c in observation.hand]
    if len(hand)!=len(set(hand)) or len(hand)!=observation.remaining[observation.my_seat]:
        raise ModelError('手牌或余牌数量不一致')
    events=[]
    current_move=None
    current_trick=None
    resolved:dict[int,Move]={}
    last_index=0
    for action in observation.history:
        if action.index <= last_index:
            raise ModelError('公开动作顺序无效')
        last_index=action.index
        player=(observation.my_seat-action.player)%4
        if action.trick != current_trick:
            current_trick=action.trick
            current_move=None
        if action.kind=='pass':
            events.append(('pass',player))
        elif action.kind=='play':
            move=_resolve_action(action,level,current_move)
            events.append(('play',player,move))
            resolved[action.index]=move
            current_move=move
        else:
            raise ModelError('不支持的公开动作')
    lead=None
    if not observation.free_lead and observation.lead_action is not None:
        lead=resolved.get(observation.lead_action.index)
        if lead is None:
            lead=_resolve_action(observation.lead_action,level,None)
    if not observation.free_lead and lead is None:
        raise ModelError('缺少当前需要跟的牌')
    left=[observation.remaining[(observation.my_seat-i)%4]for i in range(4)]
    legal=gen_moves(hand,level,lead)
    if not legal:
        raise ModelError('未生成合法动作')
    return {'hand':hand,'level':level,'player':0,'left':left,'done':[x==0 for x in left],
            'events':events,'lead':lead,'lead_owner':None,'legal':legal}


class FableDanAdvisor:
    def __init__(self, weights_path: Path | str, *, top_n: int=3, batch_size: int=128):
        self.weights_path=Path(weights_path)
        self.top_n=max(1,min(top_n,5))
        self.batch_size=max(16,min(batch_size,512))
        self.model:NumpyModel|None=None
        self.fingerprint=''
        self.metadata:dict[str,Any]={}

    def load(self) -> dict[str,Any]:
        if self.model is not None:
            return dict(self.metadata)
        if not self.weights_path.is_file():
            raise ModelError('未找到模型文件，请在设置中导入 best.npz')
        started=perf_counter()
        try:
            with np.load(self.weights_path,allow_pickle=False)as archive:
                weights={key:archive[key]for key in archive.files}
            model=NumpyModel(weights)
            if int(model.cfg.get('feat_dim',FEAT_DIM))!=FEAT_DIM or int(model.cfg.get('vocab',VOCAB))!=VOCAB:
                raise ModelError('模型特征或词表版本不兼容')
            for name,array in model.w.items():
                if not isinstance(array,np.ndarray) or array.dtype.kind!='f' or not np.isfinite(array).all():
                    raise ModelError('模型包含无效的数值数组')
            self.fingerprint=hashlib.sha256(self.weights_path.read_bytes()).hexdigest()
            self.model=model
            self.metadata={'status':'loaded','backend':'numpy','model_sha256':self.fingerprint,
                           'load_ms':round((perf_counter()-started)*1000,2),
                           'parameter_arrays':len(model.w),'config':dict(model.cfg)}
            return dict(self.metadata)
        except ModelError:
            raise
        except Exception as exc:
            raise ModelError('模型文件损坏或结构不兼容')from exc

    def recommend(self, observation: GameObservation, request_id: str='') -> AdviceResult:
        started=perf_counter()
        try:
            self.load()
            obs=build_model_observation(observation)
            tokens,features=encode_decision(obs)
            assert self.model is not None
            # Compute the history context once, and limit action-head working memory.
            context=self.model.context(tokens)
            values=[]
            for start in range(0,len(features),self.batch_size):
                feat=np.asarray(features[start:start+self.batch_size],dtype=np.float32)
                hand_embedding=self.model._mlp(feat,'hand_mlp.',None)
                repeated=np.broadcast_to(context,(len(feat),len(context)))
                q=self.model._mlp(np.concatenate([repeated,hand_embedding],axis=-1),'q_head.',None)[:,0]
                values.extend(float(x)for x in q)
            if not np.isfinite(values).all():
                raise ModelError('模型返回了无效评分')
            order=sorted(range(len(values)),key=lambda i:(-values[i],i))[:self.top_n]
            candidates=[]
            for index in order:
                move=obs['legal'][index]
                cards=tuple(from_fable(int(c))for c in move.cards)
                if Counter(cards)-Counter(observation.hand):
                    raise ModelError('模型动作不属于当前手牌')
                candidates.append(AdviceCandidate(cards=cards,kind=str(move.type),
                    label=TYPE_LABELS[move.type],score=values[index],claim=tuple(move.claim_ranks)))
            return AdviceResult(session_id=observation.session_id,game_id=observation.game_id,
                state_version=observation.state_version,request_id=request_id,candidates=tuple(candidates),
                elapsed_ms=(perf_counter()-started)*1000,model_fingerprint=self.fingerprint)
        except Exception as exc:
            message=str(exc) if isinstance(exc,ModelError) else '推荐计算失败，请查看诊断信息'
            return AdviceResult(session_id=observation.session_id,game_id=observation.game_id,
                state_version=observation.state_version,request_id=request_id,
                elapsed_ms=(perf_counter()-started)*1000,status='error',error_code='MODEL_INPUT_OR_INFERENCE',
                error_message=message,model_fingerprint=self.fingerprint)
