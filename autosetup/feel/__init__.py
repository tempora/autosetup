"""How you want the car to feel: two toggles, a locked fingerprint, and transfer.

cars.py         which car a recording is (stable across liveries, seasons and game builds)
measure.py      the balance a stint measured, per phase × speed band (cells)
target.py       the balance the optimiser aims for: a stint's balance plus your toggles on it
fingerprint.py  "This is it": the balance you lock, with its tolerance and aero platform,
                and what it becomes on another track or another car of the class
"""
