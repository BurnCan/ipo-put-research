from app.services.sec_edgar import parse_master_index


def test_parse_master_index():
    sample = '''Description: Master Index of EDGAR Dissemination Feed\nCIK|Company Name|Form Type|Date Filed|Filename\n--------------------------------------------------------------------------------\n12345|Example Corp|S-1|2026-08-01|edgar/data/12345/0000012345-26-000001.txt\n99999|Other Corp|10-K|2026-08-02|edgar/data/99999/0000099999-26-000001.txt\n'''
    rows = parse_master_index(sample)
    assert len(rows) == 1
    assert rows[0].cik == '0000012345'
    assert rows[0].form_type == 'S-1'
