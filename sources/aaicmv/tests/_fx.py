import os
FX = os.path.join(os.path.dirname(__file__), "fixtures")
def load(name):
    with open(os.path.join(FX, name), encoding="utf-8") as fh:
        return fh.read()
