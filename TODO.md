# autosetup TODO

## Open

- [ ] **Goal: learn what you generally like, so it can one-shot setups in your range.** Over
  time the app should know you well enough that the first proposal on a new car or track
  already lands in the balance and feel you like, instead of creeping there stint by stint.
  - [ ] Keep your liking as "you vs. usual" (rating points away from each class's usual
    balance, per zone) as well as per class, so it carries to a class you haven't driven.
  - [ ] First proposal on a new car or track: jump straight into your range, sized from how
    far the car measured from it, not small steps.
  - [ ] Use every learned preference from the first proposal: balance, car feel, countersteer
    you accept, brake pressure comfort, and the settings you tend to move towards (the
    Driver screen shows those; proposals don't use them yet).
  - [ ] It still needs one short stint to measure where a new car sits; with a learned
    profile, a couple of laps should be enough to aim the next setup.

- [ ] **Learn what's fast from where on the lap you gain and lose time, not from lap averages.**
  Today the optimiser only sees each stint's average lap time. At Barcelona (LMP2, 6 stints)
  that average is ±0.3 s noisy and it knew the lap-time effect of 0 of 29 settings
  (confidence in its pick: 47 %). Split into 100 m sections the noise is ~23 ms, and the
  differences are clear: one setup was faster in 7 sections and slower in none (−0.31 s),
  others traded time between sections.
  - [ ] Tag each 100 m section by what it is: **straight** (full throttle, no cornering) or
    corner speed (low / mid / high) × phase (entry / mid / exit), from your own speed and
    pedal traces.
  - [ ] Per stint, time gained or lost per section type against your typical time, counted
    only when clearly beyond the lap-to-lap noise.
  - [ ] Take your own improvement out first (experience correction), so a later stint isn't
    credited to the setup just because you've learned the track.
  - [ ] **Straight-line speed** as evidence of its own: time through straight sections and
    speed at the end of each straight, per setup. That's the trade-off wing and rake make
    against cornering speed, and lap time alone hides it (+0.2 s on straights and −0.2 s in
    fast corners looks like "no change").
  - [ ] Use this as the optimiser's pace signal, combined with the balance you like: which
    changes made you faster in which kind of corner, and on the straights.
  - [ ] **Find the best compromise, not one number.** Most changes trade time in one part of
    the lap for another (less wing: straights vs fast corners; a stiffer bar: fast corners
    vs kerbs; more diff lock: exits vs entry rotation). Weigh the gains and losses across
    section types and pick the best overall, but only among setups you can still drive and
    like to drive: within the balance you like, the countersteer you accept, and your
    consistency. (Today's Pareto flag only marks stints not beaten on pace, wear and
    consistency together; it isn't this.)

- [ ] **How you like to drive is a hard limit, not a cost to trade for lap time.** It must never
  push someone into a more oversteery car because that's faster, if they can't drive it.
  Today the balance outside your liked range is only a soft cost (~0.1 s of drivability per
  rating point), so a big enough predicted pace gain could outweigh it.
  - [ ] Your liked range is a fence: setups predicted beyond it aren't considered, however
    fast they look; within it, optimise for speed.
  - [ ] The fence follows the person, both ways. It only stops the app pushing someone past
    what they like; it must never stop anyone getting the car they want. Someone who likes a
    really oversteery car gets a range far out on the oversteer side, and the app aims there.
  - [ ] Car feel must reach that far: today Pointier / Looser only move one rating point.
    Allow several steps each way, so a very loose (or very lazy) car can be asked for.
  - [ ] Watch whether you're coping: if a setup makes your laps less consistent, adds offs or
    lots of countersteer, that's evidence you can't handle it, even if the best lap was
    quicker. Pull the fence back, towards what you've shown you can drive.

- [ ] **Code cleanup.** Remove AI fluff and keep the code readable.
  - [ ] Cut verbose, over-explaining comments and docstrings down to what a reader needs.
  - [ ] Shorten the READMEs (main and portable): how to run, build and use it, not essays.
  - [ ] Remove dead code and leftovers from replaced designs (e.g. the cross-track transfer
    code the app no longer uses, the old phase / speed rating columns once migrated).

- [ ] **Is it actually learning what's fast, for how you drive?** Answered: no, not from lap
  averages (above). Re-check once section-based learning is in.

## Done
