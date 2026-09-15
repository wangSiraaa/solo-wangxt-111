"""全部 HTTP 测试共享一个临时 SQLite 库（在导入 app 前固定连接串）。"""
import os
import tempfile

_tmp = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
_tmp.close()
os.environ["BATCH_DATABASE_URL"] = f"sqlite:///{_tmp.name}"
