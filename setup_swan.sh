#!/usr/bin/env bash
# Run from a SWAN terminal: bash setup_swan.sh
# Use `source setup_swan.sh` to also export the settings into this terminal.

_sidm_setup_swan() {
    local sidm_repo sidm_env sidm_python sidm_env_file
    sidm_repo="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)" || return 1
    if [[ "${1:-}" == "--help" ]]; then
        printf '%s\n' 'Usage: bash setup_swan.sh [--use-existing-proxy]' \
            'Default: run normal setup, request a 192-hour CMS proxy, test FNAL, and register Python (SIDM SWAN).' \
            'Put your CMS certificate/key in ~/.globus first. Enter the passphrase only in this terminal.' \
            '--use-existing-proxy: test and reuse a proxy you already created or copied; does not extend its lifetime.' \
            'Select Python (SIDM SWAN) in JupyterLab afterwards. Restart an already-running kernel.'
        return 0
    fi
    if [[ $# -gt 1 || ( $# -eq 1 && "$1" != "--use-existing-proxy" ) ]]; then
        printf '%s\n' 'Unknown option. Run: bash setup_swan.sh --help' >&2
        return 1
    fi
    export RUNNING_ON_SWAN=false SIDM_SWAN_READY=false
    sidm_env="${VIRTUAL_ENV:-${HOME}/SIDM_env}"
    if [[ ! -x "$sidm_env/bin/python" ]]; then
        printf '%s\n' "Preparing a private Python environment in $sidm_env ..."
        python3 -m venv "$sidm_env" || return 1
    fi
    sidm_python="$sidm_env/bin/python"
    # The normal setup uses `pip`, so ensure it installs into this environment.
    export PATH="$sidm_env/bin:$PATH"
    printf '\n%s\n' '[1/5] Running the normal SIDM setup (setup.sh) ...'
    (cd -- "$sidm_repo" && bash -e setup.sh) || {
        printf '%s\n' 'Normal setup failed. The error above must be resolved before SWAN setup can continue.' >&2
        return 1
    }
    printf '\n%s\n' '[2/5] Checking that this terminal belongs to SWAN ...'
    "$sidm_python" "$sidm_repo/sidm/tools/swan.py" --check-platform || return 1
    export RUNNING_ON_SWAN=true
    if ! "$sidm_python" -c 'import coffea, awkward, hist, uproot, XRootD, ipykernel; from sidm.tools import sidm_processor, llpnanoaodschema, utilities' >/dev/null 2>&1; then
        printf '%s\n' 'Installing the analysis packages listed by this project. This can take several minutes ...'
        "$sidm_python" -m pip install -r "$sidm_repo/requirements.txt" ipykernel || return 1
    fi
    "$sidm_python" "$sidm_repo/sidm/tools/swan.py" "$@" || return 1
    sidm_env_file="/tmp/sidm-swan-$(id -u)/environment.sh"
    # Written by the helper with mode 600, containing exports only (no credentials).
    source "$sidm_env_file" || return 1
    printf '\n%s\n' 'Ready! In your notebook choose Kernel > Change Kernel > Python (SIDM SWAN).' \
        'Restart it if it was already running, then run the notebook Setup cells.' \
        'These settings are attached to that kernel, so they work across notebooks.' \
        'To renew later, run this script again. A SWAN server reset may also require rerunning it.'
}

_sidm_setup_swan "$@"
