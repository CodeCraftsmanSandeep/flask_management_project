from app.backtest.sources import HUGGING_FACE_URL, describe_source


def test_the_hugging_face_import_is_named_with_a_link_and_its_caveat() -> None:
    info = describe_source("csv_import", ["huggingface"])
    assert info is not None
    assert info["kind"] == "huggingface"
    assert info["url"] == HUGGING_FACE_URL
    assert "not been checked against NSE" in info["note"]


def test_the_label_match_ignores_case_and_other_labels() -> None:
    assert describe_source("csv_import", ["vendor", "HuggingFace"])["kind"] == "huggingface"  # type: ignore[index]


def test_another_csv_label_is_shown_as_given_without_a_link() -> None:
    info = describe_source("csv_import", ["vendor-x"])
    assert info is not None
    assert info["kind"] == "csv"
    assert "vendor-x" in info["title"]
    assert info["url"] is None


def test_no_data_and_simulated_data_have_no_description() -> None:
    assert describe_source(None, []) is None
    assert describe_source("synthetic_demo", []) is None


def test_data_downloaded_from_a_broker_is_described_plainly() -> None:
    info = describe_source("broker_download", [])
    assert info is not None
    assert info["kind"] == "broker"
