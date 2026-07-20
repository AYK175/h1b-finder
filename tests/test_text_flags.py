from h1b_finder.jobs.text_flags import detect_sponsorship_flags


def test_no_sponsorship_language():
    flags = detect_sponsorship_flags("Applicants must be authorized to work in the US without sponsorship.")
    assert flags["mentions_no_sponsorship"] is True
    assert flags["mentions_clearance"] is False
    assert flags["mentions_sponsorship_ok"] is False


def test_clearance_language():
    flags = detect_sponsorship_flags("Active TS/SCI security clearance required.")
    assert flags["mentions_clearance"] is True
    assert flags["mentions_no_sponsorship"] is False


def test_us_citizen_required_counts_as_clearance():
    flags = detect_sponsorship_flags("Must be a US Citizen due to federal contract requirements.")
    assert flags["mentions_clearance"] is True


def test_sponsorship_ok_language():
    flags = detect_sponsorship_flags("We will sponsor visas for qualified candidates.")
    assert flags["mentions_sponsorship_ok"] is True
    assert flags["mentions_no_sponsorship"] is False


def test_no_flags_on_neutral_text():
    flags = detect_sponsorship_flags("We are looking for a talented engineer to join our team.")
    assert not any(flags.values())


def test_none_description():
    flags = detect_sponsorship_flags(None)
    assert not any(flags.values())
