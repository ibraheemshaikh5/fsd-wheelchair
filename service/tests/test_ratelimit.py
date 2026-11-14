from drive_service.ratelimit import RateWindow, admit


def test_admits_up_to_the_limit_within_one_second():
    window = RateWindow()
    results = []
    for i in range(4):
        allowed, window = admit(window, now=10.0 + i * 0.01, limit_per_s=3)
        results.append(allowed)
    assert results == [True, True, True, False]


def test_old_events_fall_out_of_the_window():
    window = RateWindow()
    for i in range(3):
        _, window = admit(window, now=10.0 + i * 0.01, limit_per_s=3)
    allowed, _ = admit(window, now=11.5, limit_per_s=3)
    assert allowed is True


def test_admit_does_not_mutate_the_window():
    window = RateWindow()
    admit(window, now=1.0, limit_per_s=3)
    assert window == RateWindow()


def test_rejected_events_are_not_remembered():
    window = RateWindow()
    _, window = admit(window, now=1.0, limit_per_s=1)
    _, after_reject = admit(window, now=1.1, limit_per_s=1)
    assert after_reject.times == (1.0,)
