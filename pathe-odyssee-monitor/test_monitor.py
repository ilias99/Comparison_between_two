import monitor


SAMPLE = [
    {
        "status": "soldout",
        "time": "2026-08-05 18:00:00",
        "version": "vost",
        "tags": ["imax", "pmr"],
        "refCmd": "https://s.pathe.fr/fr/V0001S1/booking",
        "auditoriumName": "IMAX",
        "auditoriumCapacity": "222",
    },
    {
        "status": "available",
        "time": "2026-08-05 21:00:00",
        "version": "vost",
        "tags": ["imax", "pmr"],
        "refCmd": "https://s.pathe.fr/fr/V0001S2/booking",
        "auditoriumName": "IMAX",
        "auditoriumCapacity": "222",
    },
]


def test_match_showtime_prefers_target_time():
    showtimes = [
        monitor.Showtime(
            time=i["time"],
            status=i["status"],
            version=i["version"],
            tags=i["tags"],
            ref_cmd=i["refCmd"],
            auditorium_name=i["auditoriumName"],
            auditorium_capacity=i["auditoriumCapacity"],
            raw=i,
        )
        for i in SAMPLE
    ]
    cfg = {"time": "21:00", "required_version": "", "required_tags_any": [], "required_tags_all": []}
    got = monitor.match_showtime(showtimes, cfg)
    assert got is not None
    assert got.hhmm == "21:00"
    assert got.status == "available"


def test_extract_free_seats_from_rows():
    payload = {
        "rows": [
            {
                "seats": [
                    {"status": "available", "type": None},
                    {"status": "taken", "type": None},
                    {"status": "available", "type": "pmr"},
                ]
            }
        ]
    }
    # PMR / wheelchair seats are ignored
    assert monitor._extract_free_seats_from_json(payload) == 1


def test_sanitize_yaml_text_handles_nbsp_and_smart_quotes():
    dirty = "alerts:\n\u00a0\u00a0telegram:\n\u00a0\u00a0\u00a0\u00a0enabled: true\n\u00a0\u00a0\u00a0\u00a0bot_token: \u201c123:ABC\u201d\n"
    clean = monitor.sanitize_yaml_text(dirty)
    assert "\u00a0" not in clean
    assert "\u201c" not in clean
    data = __import__("yaml").safe_load(clean)
    assert data["alerts"]["telegram"]["bot_token"] == "123:ABC"


def test_is_alertable_with_seat_count():
    show = monitor.Showtime(
        time="2026-08-05 21:00:00",
        status="available",
        version="vost",
        tags=["imax"],
        ref_cmd="https://s.pathe.fr/fr/x/booking",
        auditorium_name="IMAX",
        auditorium_capacity=222,
        raw={},
    )
    result = monitor.CheckResult(
        matched=True,
        showtime=show,
        session_bookable=True,
        free_seats=3,
        booking_url=show.ref_cmd,
        detail="ok",
        all_showtimes=[show],
    )
    assert monitor.is_alertable(result, 1) is True
    assert monitor.is_alertable(result, 5) is False


def test_parse_places_libres():
    assert monitor.parse_places_libres("Sélectionnez vos places\n0 place libre\nÉcran") == 0
    assert monitor.parse_places_libres("2 places libres") == 2
    assert monitor.parse_places_libres("Complet") == 0


def test_should_send_alert_only_on_transition():
    show = monitor.Showtime(
        time="2026-08-05 21:00:00",
        status="available",
        version="vost",
        tags=["imax"],
        ref_cmd="https://s.pathe.fr/fr/x/booking",
        auditorium_name="IMAX",
        auditorium_capacity=228,
        raw={},
    )
    # Sticky available + unknown seats must NOT alert (false positives)
    unknown = monitor.CheckResult(
        matched=True,
        showtime=show,
        session_bookable=True,
        free_seats=None,
        booking_url=show.ref_cmd,
        detail="ok",
        all_showtimes=[show],
    )
    send, reason = monitor.should_send_alert(
        unknown, 1, {"alertable": False, "free_seats": None}, True, True
    )
    assert send is False
    assert "not confirmed" in reason

    # Explicit 0 place libre must NOT alert
    zero = monitor.CheckResult(
        matched=True,
        showtime=show,
        session_bookable=True,
        free_seats=0,
        booking_url=show.ref_cmd,
        detail="ok",
        all_showtimes=[show],
    )
    send, reason = monitor.should_send_alert(
        zero, 1, {"alertable": False, "free_seats": None}, True, True
    )
    assert send is False

    # Confirmed free seats -> alert on rising edge
    free = monitor.CheckResult(
        matched=True,
        showtime=show,
        session_bookable=True,
        free_seats=2,
        booking_url=show.ref_cmd,
        detail="ok",
        all_showtimes=[show],
    )
    send, reason = monitor.should_send_alert(
        free, 1, {"alertable": False, "free_seats": 0}, True, True
    )
    assert send is True
    assert "transition" in reason

    # Still same availability -> no spam
    send, reason = monitor.should_send_alert(
        free, 1, {"alertable": True, "free_seats": 2}, True, True
    )
    assert send is False
    assert "already alerted" in reason
