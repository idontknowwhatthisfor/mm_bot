"""The smallest possible bot: it takes the first upgrade each week and builds nothing.
Every score should beat this one."""


class Bot:
    def step(self, game):
        if game.upgrade_choices:
            game.choose_upgrade(0)
