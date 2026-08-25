"""Ep UTF-8 cho moi thao tac doc/ghi text trong lúc chạy test.

VAN DE:
  reports/*.md va cac log .jsonl deu la UTF-8 (co tieng Viet), nhung
  tests/test_rto_evidence.py goi Path.read_text() / open() KHONG truyen encoding.
  Tren Windows, encoding mac dinh la cp1252 -> UnicodeDecodeError, test truot du
  noi dung file hoan toan dung. Loi nay khong lien quan gi toi bai lam trong dr/.

TAI SAO DAT O DAY:
  Cach dat bien moi truong (PYTHONUTF8=1 trong Activate.ps1) chi co tac dung neu
  terminal duoc activate LAI sau khi sua -- de quen, va mat khi environment reset.
  pytest thi LUON tu dong nap conftest.py o goc rootdir truoc khi chay bat ky test
  nao, nen fix o day co tac dung ngay, khong phu thuoc shell hay bien moi truong,
  va di kem repo nen may khac cung chay duoc.
"""
import pathlib

import pytest


def _utf8_default(func):
    def wrapper(self, encoding=None, *args, **kwargs):
        return func(self, encoding or "utf-8", *args, **kwargs)
    return wrapper


# Chi doi MAC DINH: loi goi da chi ro encoding van duoc ton trong.
pathlib.Path.read_text = _utf8_default(pathlib.Path.read_text)
pathlib.Path.write_text = _utf8_default(pathlib.Path.write_text)


@pytest.fixture(autouse=True)
def _khong_lam_ban_evidence(tmp_path, monkeypatch):
    """Giu reports/failover-events.jsonl sach khi chay test.

    test_failover_khong_cutover_khi_target_chua_ready goi ham failover() THAT, ma
    dr/failover.py ghi thang vao reports/failover-events.jsonl -- dung file evidence
    dung de cham diem. Moi lan chay `pytest tests/` la file phinh them 3 dong rac,
    lan sau doc log khong con phan biet duoc dau la drill that.

    Tro LOG sang file tam trong luc test chay: ham van ghi log binh thuong (test van
    kiem tra duoc hanh vi), nhung evidence that khong bi dung toi.
    """
    try:
        from dr import failover
    except Exception:
        return  # chua viet xong dr/failover.py -- khong can co lap gi ca
    monkeypatch.setattr(failover, "LOG", tmp_path / "failover-events.jsonl")
