"""Update bootstrap: the README install command is the update path."""
import updater


def test_bootstrap_command_is_a_fixed_powershell_argv():
    cmd = updater.github_install_command()
    assert cmd[0] == "powershell"
    assert "-NoProfile" in cmd
    assert "-Command" in cmd
    assert updater.BOOTSTRAP_URL.startswith("https://")
    script = cmd[-1]
    assert updater.BOOTSTRAP_URL in script
    assert "-NoRun" in script


def test_install_target_travels_through_the_environment():
    cmd = updater.github_install_command("OTHER_TARGET")
    assert "$env:OTHER_TARGET" in cmd[-1]
    assert "D:\\VibeForma" not in " ".join(cmd)
