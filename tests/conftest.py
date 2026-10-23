"""测试夹具：真实发票样本目录 + 临时数据根。"""

import glob
import os
import tempfile
from pathlib import Path

import pytest

# 真实发票样本是私有数据，不在仓库里。把环境变量 TIDOC_INVOICE_SAMPLES 指向存放样本的目录即可运行
# 依赖样本的测试（XML 放在目录下任意层级，PDF 命名为 *发票.pdf，付款截图命名为 *付款截图.jpg）；
# 没有设置时这些测试会跳过。
SAMPLES_ENV = "TIDOC_INVOICE_SAMPLES"
SAMPLES_SKIP_REASON = (
    f"未设置环境变量 {SAMPLES_ENV}（指向存放真实发票样本的目录），跳过依赖真实发票样本的测试"
)


def invoice_samples_dir() -> Path | None:
    """环境变量指向的样本目录；未设置或目录不存在时返回 None。"""
    value = os.environ.get(SAMPLES_ENV, "").strip()
    path = Path(value).expanduser() if value else None
    return path if path is not None and path.is_dir() else None


@pytest.fixture
def sample_dir():
    path = invoice_samples_dir()
    if path is None:
        pytest.skip(SAMPLES_SKIP_REASON)
    return path


def pytest_configure(config):
    # Windows CI checks the repo out on D: but keeps TEMP on C:. Tests that combine repo
    # resources with temporary files need both on one drive, so default basetemp to the repo.
    if os.name == "nt" and not config.option.basetemp:
        config.option.basetemp = Path(__file__).resolve().parents[1] / ".pytest_tmp"


@pytest.fixture
def sample_xmls(sample_dir):
    files = sorted(glob.glob(f"{sample_dir}/**/*.xml", recursive=True))
    if not files:
        pytest.skip(f"{SAMPLES_ENV} 指向的目录里没有发票 XML 样本")
    return files


@pytest.fixture
def sample_pdfs(sample_dir):
    files = sorted(glob.glob(f"{sample_dir}/*发票.pdf"))
    if not files:
        pytest.skip(f"{SAMPLES_ENV} 指向的目录里没有 *发票.pdf 样本")
    return files


@pytest.fixture
def api():
    from tidoc.api import Api
    api = Api(tempfile.mkdtemp())
    scheme = next(s for s in api.adapters.list_schemes() if s["package_id"]=="org.bitfsae.reimbursement")
    api.adapters.complete_adapter_setup(scheme["id"])
    return api


@pytest.fixture
def repos():
    from tidoc.db import AttachmentRepo, BatchRepo, Database, DataRoot, EntryRepo, ProfileRepo
    root = DataRoot(tempfile.mkdtemp())
    db = Database(root.db_path)
    return {
        "root": root,
        "db": db,
        "profiles": ProfileRepo(db),
        "entries": EntryRepo(db),
        "attachments": AttachmentRepo(db, root),
        "batches": BatchRepo(db),
    }
