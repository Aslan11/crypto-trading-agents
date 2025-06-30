from tools import AsciiLineChart


def test_ascii_chart_constant():
    chart = AsciiLineChart(width=5, color=False)
    for _ in range(3):
        chart.add(100)
    out = chart.render()
    lines = out.splitlines()
    assert len(lines) == 13
    assert lines[0].startswith("H 100")
    assert lines[5].endswith("●")
    assert lines[-2].strip() == "► Time →"
    assert lines[-1].strip() == "t-4…t0"
