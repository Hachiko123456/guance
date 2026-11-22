"""Legal move list backed by the bundled Python rule implementation."""

from .python_rules import first_actions, second_actions
from .types import Move


class Moves:
    def __init__(self):
        self.valid_range = range(0, 1)
        self.action_list = []

    def __len__(self):
        return len(self.action_list)

    def __getitem__(self, item):
        return Move(*self.action_list[item])

    def parse_first_action(self, hand_cards, hearts_num, current_rank):
        if not hand_cards:
            self.action_list = []
        else:
            self.action_list = first_actions(hand_cards, hearts_num, current_rank)
        self.valid_range = range(0, len(self.action_list))

    def parse_second_action(
        self,
        hand_cards,
        hearts_num,
        current_rank,
        greater_action,
        rank_order,
        number_order,
    ):
        self.action_list = second_actions(
            hand_cards,
            hearts_num,
            current_rank,
            greater_action,
        )
        self.valid_range = range(0, len(self.action_list))


# Vendored for Guance danks_v3 from DanKS-Train guandan/engine/moves.py
# reference commit c618f537c7a83c77b76c3082985e42efff67ec02; original sha256 c74e6568bc4da598efb45b784417ebb554033626e140eaa9abf55fb819f9c85e;
# only absolute import roots were rewritten (DanKS./guandan. -> vendored package roots).
