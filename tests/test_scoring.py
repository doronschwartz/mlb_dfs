from mlb_dfs.scoring import HitterLine, PitcherLine, _ip_to_outs


def test_ip_to_outs():
    assert _ip_to_outs("6.0") == 18
    assert _ip_to_outs("6.1") == 19
    assert _ip_to_outs("6.2") == 20
    assert _ip_to_outs(7) == 21
    assert _ip_to_outs(0) == 0


def test_hitter_line_2hr_3rbi_2r_1bb():
    # 2 HR, 3 RBI, 2 R, 1 BB, 1 K -> 2*10 + 3*2 + 2*2 + 1*2 + 1*-1 = 20+6+4+2-1 = 31
    line = HitterLine.from_mlb_stats({
        "hits": 2, "doubles": 0, "triples": 0, "homeRuns": 2,
        "runs": 2, "rbi": 3, "baseOnBalls": 1, "strikeOuts": 1,
    })
    assert line.singles == 0
    assert line.points() == 31.0


def test_hitter_line_singles_and_double():
    # 3 H = 2 singles + 1 double, 1 R, 1 RBI, 1 SB
    # 2*3 + 1*5 + 1*2 + 1*2 + 1*3 = 6+5+2+2+3 = 18
    line = HitterLine.from_mlb_stats({
        "hits": 3, "doubles": 1, "runs": 1, "rbi": 1, "stolenBases": 1,
    })
    assert line.singles == 2
    assert line.points() == 18.0


def test_pitcher_quality_start():
    # 6 IP, 7 K, 2 ER, 5 H, 1 BB
    # outs = 18 -> 18*0.75 = 13.5
    # 7*1.5 = 10.5; 2*-2 = -4; 5*-0.6 = -3.0; 1*-0.6 = -0.6
    # QS bonus +4 (>=18 outs, <=3 ER)
    # total = 13.5 + 10.5 - 4 - 3 - 0.6 + 4 = 20.4
    line = PitcherLine.from_mlb_stats({
        "inningsPitched": "6.0",
        "strikeOuts": 7, "earnedRuns": 2, "hits": 5, "baseOnBalls": 1,
    })
    assert line.is_quality_start()
    assert round(line.points(), 2) == 20.4


def test_pitcher_blowup_no_qs():
    # 4.2 IP (14 outs), 3 K, 6 ER, 9 H, 3 BB
    # 14*0.75 = 10.5; 3*1.5 = 4.5; 6*-2 = -12; 9*-0.6 = -5.4; 3*-0.6 = -1.8
    # no QS, no bonuses -> -4.2
    line = PitcherLine.from_mlb_stats({
        "inningsPitched": "4.2",
        "strikeOuts": 3, "earnedRuns": 6, "hits": 9, "baseOnBalls": 3,
    })
    assert not line.is_quality_start()
    assert round(line.points(), 2) == -4.2
