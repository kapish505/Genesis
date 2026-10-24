import py_compile

from genesis.builder import Builder
from tests.test_spec import sample


def test_builder_emits_python_and_badges(tmp_path):
    path, readme = Builder().build_files(sample(), tmp_path, "http://gateway", "token")
    py_compile.compile(str(path), doraise=True)
    text = readme.read_text()
    assert "tag:innovationlab" in text and "tag:hackathon" in text
    source = path.read_text()
    assert "def hosted_execute" in source
    assert "from genesis" not in source
    assert "Agent()" not in source
