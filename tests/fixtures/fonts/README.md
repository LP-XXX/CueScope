# Synthetic layout test font

`CueScopeFixtureSans.otf` contains the characters used by the synthetic chat-layout
fixtures and printable ASCII. It is a renamed subset of **Noto Sans CJK SC Regular**,
distributed under the [SIL Open Font License 1.1](OFL.txt). The original copyright
notices are preserved in the font. The font is a test input and is excluded from
the portable application.

The source is [Noto CJK, revision f8d1575](https://github.com/notofonts/noto-cjk/blob/f8d157532fbfaeda587e826d4cd5b21a49186f7c/Sans/OTF/SimplifiedChinese/NotoSansCJKsc-Regular.otf),
SHA-256 `2c76254f6fc379fddfce0a7e84fb5385bb135d3e399294f6eeb6680d0365b74b`.
FontTools 4.55.3 retained the glyphs from the string constants in
`test_acceptance_021.py`, `test_acceptance_final_021.py` and `test_message_split.py`,
plus printable ASCII. The family name and PostScript name were changed to
`CueScope Fixture Sans` and `CueScopeFixtureSans-Regular`.
