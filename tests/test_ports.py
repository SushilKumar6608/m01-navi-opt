from navi_opt.grid.ports import load_ports


def test_ports_load_and_have_valid_coordinates():
    ports = load_ports()
    assert len(ports) >= 10
    for port in ports.values():
        assert -180 <= port.lon <= 180
        assert -90 <= port.lat <= 90
        assert port.name
        assert port.country


def test_known_port_present():
    ports = load_ports()
    assert "rotterdam" in ports
    assert ports["rotterdam"].country == "NL"
