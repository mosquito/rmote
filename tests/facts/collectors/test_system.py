from rmote.tools.facts.collectors import SystemFacts
from rmote.tools.facts.collectors.system import DistributionInfo


def test_distribution_reads_current_data_and_fallback(tmp_path):
    primary, fallback = tmp_path / "primary", tmp_path / "fallback"
    paths = (primary, fallback)
    assert SystemFacts.read_distribution(paths) is None
    fallback.write_text('ID=debian\nVERSION_ID="13"\nPRETTY_NAME="Debian GNU/Linux"\n')
    assert SystemFacts.read_distribution(paths) == DistributionInfo("debian", "13", "Debian GNU/Linux")
    primary.write_text("ID=arch\n")
    assert SystemFacts.read_distribution(paths) == DistributionInfo("arch")
    primary.write_text("ID=alpine\nVERSION_ID='3.23'\n")
    result = SystemFacts.read_distribution(paths)
    assert result is not None and result.version_id == "3.23"
