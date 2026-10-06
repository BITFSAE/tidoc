"""测试夹具：真实发票样本目录 + 临时数据根。"""

import glob
import tempfile

import pytest

SAMPLE_DIR = "/Users/poli/invoice2docx/invoices"


@pytest.fixture
def sample_xmls():
    files = sorted(glob.glob(f"{SAMPLE_DIR}/**/*.xml", recursive=True))
    if not files:
        pytest.skip("无发票 XML 样本")
    return files


@pytest.fixture
def sample_pdfs():
    files = sorted(glob.glob(f"{SAMPLE_DIR}/*发票.pdf"))
    if not files:
        pytest.skip("无发票 PDF 样本")
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
