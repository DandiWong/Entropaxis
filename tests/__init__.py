"""Entropaxis system test package."""
import atexit
import os
import shutil
import tempfile

# 回执库隔离：被测工具（含子进程）一律写临时目录，不污染 data/receipts/。
_RECEIPTS = tempfile.mkdtemp(prefix="entropaxis-test-receipts-")
os.environ["ENTROPAXIS_RECEIPT_DIR"] = _RECEIPTS
atexit.register(shutil.rmtree, _RECEIPTS, True)
