"""Werewolf game engine — pure logic, no Flask, no UI."""


class GameEngine:
    def __init__(self, players, roles):
        self.players = list(players)
        self.roles = list(roles)
        self.alive = [True] * len(players)
        self.log = []
        self.winner = None
        # Witch potion tracking
        self.witch_heal_used = False
        self.witch_poison_used = False

    # ── Helpers ──
    def alive_indices(self):
        return [i for i, a in enumerate(self.alive) if a]

    def alive_werewolves(self):
        return [i for i in self.alive_indices() if self.roles[i] == 'werewolf']

    def alive_villagers(self):
        return [i for i in self.alive_indices() if self.roles[i] != 'werewolf']

    def find_role_index(self, role):
        for i in self.alive_indices():
            if self.roles[i] == role:
                return i
        return None

    # ── Night resolution (handles wolf + sheriff + witch + doctor) ──
    def resolve_night_actions(self, actions):
        """
        actions keys:
            'werewolf_kill': idx or None
            'sheriff_kill': idx or None
            'witch_heal': idx or None
            'witch_poison': idx or None
            'doctor_save': idx or None
        Returns LIST of killed indices.
        """
        killed = []
        wolf_target    = actions.get('werewolf_kill')
        sheriff_target = actions.get('sheriff_kill')
        witch_heal     = actions.get('witch_heal')
        witch_poison   = actions.get('witch_poison')
        doctor_save    = actions.get('doctor_save')

        # 1. Werewolf kill (saved by doctor OR witch heal)
        if wolf_target is not None:
            saved = (wolf_target == doctor_save) or (wolf_target == witch_heal)
            if not saved and wolf_target in self.alive_indices():
                self.alive[wolf_target] = False
                killed.append(wolf_target)

        # 2. Sheriff kill (saved by doctor only — NOT witch)
        if sheriff_target is not None and sheriff_target != doctor_save:
            if sheriff_target in self.alive_indices() and sheriff_target not in killed:
                self.alive[sheriff_target] = False
                killed.append(sheriff_target)

        # 3. Witch poison (NO save possible)
        if witch_poison is not None:
            if witch_poison in self.alive_indices() and witch_poison not in killed:
                self.alive[witch_poison] = False
                killed.append(witch_poison)

        return killed

    # ── Voting ──
    def resolve_vote(self, target_idx):
        if target_idx is None:
            return None
        if target_idx in self.alive_indices():
            self.alive[target_idx] = False
            return target_idx
        return None

    # ── Win check ──
    def check_win(self):
        wolves = len(self.alive_werewolves())
        villagers = len(self.alive_villagers())
        if wolves == 0:
            self.winner = 'village'
            return 'village'
        if wolves >= villagers:
            self.winner = 'werewolf'
            return 'werewolf'
        return None

    # ── Serialization ──
    def to_dict(self):
        return {
            'players': self.players,
            'roles': self.roles,
            'alive': self.alive,
            'log': self.log,
            'winner': self.winner,
            'witch_heal_used': self.witch_heal_used,
            'witch_poison_used': self.witch_poison_used,
        }

    @classmethod
    def from_dict(cls, data):
        e = cls(data['players'], data['roles'])
        e.alive = data['alive']
        e.log = data.get('log', [])
        e.winner = data.get('winner')
        e.witch_heal_used = data.get('witch_heal_used', False)
        e.witch_poison_used = data.get('witch_poison_used', False)
        return e