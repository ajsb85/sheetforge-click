from sheetforge_click.click_schematic import extract_schematic, schematic_sections


def test_connectivity_follows_sheet_and_net_bookmarks():
    outline = [
        (1, "Board documentation", -1),
        (2, 'Schematic("All Documents",Physical)', 1),
        (3, "Power.SchDoc(Power)", 1),
        (4, "Components", -1),
        (5, "Q2A", 1),
        (6, "Q2-6", 1),
        (5, "Q2B", 1),
        (6, "Q2-5", 1),
        (4, "Nets", -1),
        (5, "VBUS", 1),
        (6, "Pins", -1),
        (7, "Q2-6", 1),
        (6, "NetLabels", -1),
        (7, "USB_VBUS", 1),
        (3, "Interface.SchDoc(Interface)", 2),
        (4, "Components", -1),
        (5, "Q2A", 2),
        (6, "Q2-6", 2),
        (4, "Nets", -1),
        (5, "SDA", 2),
        (6, "Pins", -1),
        (7, "Q2-6", 2),
    ]

    schematic = extract_schematic(outline)
    power, interface = schematic["sheets"]
    vbus = power["nets"][0]
    assert vbus["pins"] == [{
        "label": "Q2-6", "page": 1, "bookmark": 12, "matching_component_bookmarks": [5],
    }]
    assert vbus["objects"][0]["children"] == [{"label": "USB_VBUS", "page": 1, "bookmark": 14}]
    assert interface["nets"][0]["pins"][0]["matching_component_bookmarks"] == [17]
    assert "`VBUS` connects `Q2-6`" in "\n".join(schematic_sections(schematic, 1))
    assert "VBUS" not in "\n".join(schematic_sections(schematic, 2))


def test_generic_bookmark_headings_do_not_assert_connectivity():
    assert extract_schematic([(1, "Components", 1), (2, "U1", 1)]) is None
    assert schematic_sections(None, 1) == []
