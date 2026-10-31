"""Setup file names and descriptions from templates."""

from autosetup import templates
from autosetup.feel.cars import model_of


def test_every_field_renders():
    fields = {"track": "Lusail", "car": "Aston Martin Vantage", "class": "GT3", "date": "2026-10-29",
              "time": "21-05", "n": 3, "stint": 5, "lap": "1-43.46", "feel": "Pointier + Looser",
              "changes": "Rear ride height 6.8 cm → 7.2 cm: more rotation in fast corners"}
    assert templates.render(templates.NAME, fields) == "Lusail Aston Martin Vantage 2026-10-29 3"
    out = templates.render("{class} {time} {stint} {lap} {feel}: {changes}", fields)
    assert out == "GT3 21-05 5 1-43.46 Pointier + Looser: Rear ride height 6.8 cm → 7.2 cm: more rotation in fast corners"
    assert templates.render("{track} {nope}", fields) == "Lusail {nope}"  # unknown: left as typed


def test_the_car_is_the_model_never_the_team():
    assert templates.car_name(model_of("GT3 AMR_LMGT3 WEC2026", "GT3")) == "Aston Martin Vantage"  # Heart of Racing #23
    assert templates.car_name("Porsche_911_GT3_R_LMGT3") == "Porsche 911 GT3 R"
    assert templates.car_name("Ferrari_296_LMGT3") == "Ferrari 296"
    # Not matched to a saved setup yet: left out with its separator.
    assert templates.render(templates.NAME, {"track": "Lusail", "car": "", "date": "2026-10-29", "n": 1}) == \
        "Lusail 2026-10-29 1"


def test_file_unsafe_names_and_laps():
    assert templates.file_safe('Spa: "GP"/Long? ') == "Spa- -GP--Long-"
    assert templates.file_safe("..Lusail.. ") == "Lusail"
    assert templates.lap_text(103.46, True) == "1-43.46" and templates.lap_text(103.46, False) == "1:43.46"
    assert templates.feel_text(0, 0) == "As it was" and templates.feel_text(-1, -1) == "Pointier + Looser"
