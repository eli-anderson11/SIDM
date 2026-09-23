"""SWAN setup and small notebook checks; importing this module has no side effects."""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import time

JAVA_FALLBACK = Path('/cvmfs/sft.cern.ch/lcg/releases/java/17.0.13p11-d5c26/x86_64-el9-gcc11-opt')
KERNEL_NAME = 'sidm_swan'
TRUE_VALUES = {'true', '1', 'yes'}


def is_swan():
    """Use SWAN-specific installation markers as well as CERNBox, not hostname."""
    return bool(os.environ.get('CERNBOX_HOME')) and (
        os.environ.get('SWAN_BIN_DIR') == '/usr/local/bin/swan'
        or Path('/usr/local/bin/swan').is_dir()
    )


def runtime_directory():
    directory = Path('/tmp') / f'sidm-swan-{os.getuid()}'
    directory.mkdir(mode=0o700, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise RuntimeError(f'Unsafe setup directory: {directory}')
    directory.chmod(0o700)
    return directory


def voms_environment():
    env = os.environ.copy()
    if not shutil.which('java') and not Path(env.get('JAVA_HOME', ''), 'bin/java').is_file():
        if not (JAVA_FALLBACK / 'bin/java').is_file():
            raise RuntimeError('Java is unavailable. Select a SWAN stack with Java or set JAVA_HOME, then retry.')
        env['JAVA_HOME'] = str(JAVA_FALLBACK)
    for command in ('voms-proxy-init', 'voms-proxy-info'):
        if not shutil.which(command):
            raise RuntimeError(f'{command} is unavailable in this SWAN environment. Ask SWAN support for the CMS VOMS client.')
    return env


def proxy_lifetimes(proxy, env=None):
    proxy = Path(proxy)
    if not proxy.is_file():
        raise RuntimeError(f'CMS proxy missing: {proxy}. Run bash setup_swan.sh in the project terminal.')
    info = proxy.stat()
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) & 0o077:
        raise RuntimeError(f'The proxy must belong to you and be private. Run: chmod 600 {shlex.quote(str(proxy))}')
    env = voms_environment() if env is None else env
    result = subprocess.run(
        ['voms-proxy-info', '-file', str(proxy), '-timeleft', '-actimeleft'],
        env=env, capture_output=True, text=True, timeout=30,
    )
    if result.returncode:
        raise RuntimeError('Cannot inspect the CMS proxy: ' + result.stderr.strip())
    seconds = [int(line) for line in result.stdout.splitlines() if line.strip().lstrip('-').isdigit()]
    if len(seconds) != 2 or min(seconds) <= 0:
        raise RuntimeError('The proxy or its VOMS membership has expired or is missing. Run bash setup_swan.sh to renew it.')
    vo = subprocess.run(
        ['voms-proxy-info', '-file', str(proxy), '-vo'],
        env=env, capture_output=True, text=True, timeout=30,
    )
    if vo.returncode or 'cms' not in vo.stdout.split():
        raise RuntimeError('This proxy does not contain CMS VOMS membership. Create it with --voms cms.')
    return seconds


def configure_notebook():
    """Return the fileset switch; leave coffea-casa authentication unchanged."""
    running = os.environ.get('RUNNING_ON_SWAN', 'false').strip().lower() in TRUE_VALUES
    if not running:
        if is_swan():
            raise RuntimeError('SWAN detected, but this kernel is not configured. Run bash setup_swan.sh, then select Python (SIDM SWAN).')
        print('File access: coffea-casa xcache (default).')
        return False
    os.environ['SIDM_SWAN_READY'] = 'false'
    if not is_swan():
        raise RuntimeError('RUNNING_ON_SWAN is set outside SWAN. Use your normal Python kernel instead.')
    receipt_path = runtime_directory() / 'ready.json'
    try:
        receipt = json.loads(receipt_path.read_text())
    except (OSError, ValueError):
        raise RuntimeError('SWAN setup has not completed for this server. Run bash setup_swan.sh.') from None
    proxy = os.environ.get('X509_USER_PROXY', '')
    if not receipt.get('ready') or receipt.get('proxy') != proxy:
        raise RuntimeError('SWAN setup is incomplete or the proxy path changed. Run bash setup_swan.sh, then restart this kernel.')
    if os.environ.get('XrdSecPROTOCOL') != 'gsi,unix':
        raise RuntimeError('This kernel has different XRootD settings. Select Python (SIDM SWAN) and restart the kernel.')
    seconds = proxy_lifetimes(proxy)
    os.environ['SIDM_SWAN_READY'] = 'true'
    print(f'File access: FNAL EOS via CMS proxy (Kerberos excluded); {min(seconds)/3600:.1f} hours remaining.')
    return True


def probe_files():
    """Read five event numbers from one of the trigger study's actual inputs."""
    import uproot
    from sidm.tools import utilities

    sample = '2Mu2E_200GeV_0p25GeV_10p0mm'
    fileset = utilities.make_fileset(
        [sample], 'llpNanoAOD_v2', max_files=1, replace_xcache=True,
        location_cfg='signal_2mu2e_v10.yaml',
    )
    filename = fileset[sample]['files'][0]
    if not filename.startswith('root://cmseos.fnal.gov/'):
        raise RuntimeError(f'The test expected the FNAL EOS endpoint, received {filename}')
    print(f'Testing trigger sample: {sample}', flush=True)
    with uproot.open(filename, timeout=20) as root_file:
        tree = root_file['Events']
        events = tree.arrays(['event'], entry_stop=5)
        if len(events) != min(5, tree.num_entries) or tree.num_entries == 0:
            raise RuntimeError('The test file did not return the expected event records.')
        print(f'FNAL read passed: {tree.num_entries} events in the file; read {len(events)} event records.', flush=True)


def write_private_json(path, value):
    # Atomic replacement avoids partially-written status and kernel files.
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.sidm-')
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2)
            stream.write('\n')
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def setup(use_existing=False):
    if not is_swan():
        raise RuntimeError('This setup is for CERN SWAN. Open a terminal inside your SWAN session and run it there.')
    directory = runtime_directory()
    write_private_json(directory / 'ready.json', {'ready': False})
    env = voms_environment()
    proxy = Path(env.get('X509_USER_PROXY', f'/tmp/x509up_u{os.getuid()}')).expanduser().absolute()
    if proxy.is_symlink():
        raise RuntimeError('Use a regular file for X509_USER_PROXY, not a symbolic link.')
    print('\n[3/5] Preparing your CMS proxy ...', flush=True)
    if use_existing:
        print('Reusing your existing proxy. Its expiration time will not be extended.', flush=True)
        proxy_lifetimes(proxy, env)
    else:
        cert = Path(env.get('X509_USER_CERT', str(Path.home() / '.globus/usercert.pem'))).expanduser()
        key = Path(env.get('X509_USER_KEY', str(Path.home() / '.globus/userkey.pem'))).expanduser()
        if not cert.is_file() or not key.is_file():
            raise RuntimeError('Certificate/key missing. Put your CMS usercert.pem and userkey.pem in ~/.globus on SWAN, then retry. If you already copied a valid CMS proxy, use bash setup_swan.sh --use-existing-proxy.')
        if proxy.resolve() in (cert.resolve(), key.resolve()):
            raise RuntimeError('X509_USER_PROXY must be a separate file from your certificate and private key.')
        if stat.S_IMODE(key.stat().st_mode) not in (0o400, 0o600):
            raise RuntimeError(f'VOMS requires private-key permissions 400 or 600. Run: chmod 600 {shlex.quote(str(key))}')
        print('Requesting 192 hours (8 days). CMS may grant a shorter lifetime.', flush=True)
        print('Enter your GRID certificate passphrase below. Nothing will appear while you type.', flush=True)
        fd, staged = tempfile.mkstemp(prefix='proxy-', dir=directory)
        os.close(fd)
        try:
            subprocess.run([
                'voms-proxy-init', '--voms', 'cms', '--valid', '192:00', '--vomslife', '192:00',
                '--cert', str(cert), '--key', str(key), '--out', staged,
            ], env=env, check=True)
            os.chmod(staged, 0o600)
            proxy_lifetimes(staged, env)
            proxy.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, proxy)
        finally:
            if os.path.exists(staged):
                os.unlink(staged)
    seconds = proxy_lifetimes(proxy, env)
    print(f'Actual lifetime: proxy {seconds[0]/3600:.1f} hours; CMS VOMS membership {seconds[1]/3600:.1f} hours.', flush=True)
    print(f'Renew before the shorter lifetime expires ({min(seconds)/3600:.1f} hours).', flush=True)
    settings = {
        'RUNNING_ON_SWAN': 'true', 'SIDM_SWAN_READY': 'true',
        'X509_USER_PROXY': str(proxy),
        'X509_CERT_DIR': env.get('X509_CERT_DIR', '/etc/grid-security/certificates'),
        'XrdSecPROTOCOL': 'gsi,unix',
    }
    if env.get('JAVA_HOME'):
        settings['JAVA_HOME'] = env['JAVA_HOME']
    print('\n[4/5] Checking a small FNAL file read without Kerberos ...', flush=True)
    probe_env = dict(env, **settings)
    # A fresh process with a nonexistent ticket cache ensures this test cannot use Kerberos.
    with tempfile.TemporaryDirectory(prefix='no-kerberos-', dir=directory) as no_kerberos:
        probe_env['KRB5CCNAME'] = 'FILE:' + str(Path(no_kerberos) / 'missing')
        subprocess.run([sys.executable, str(Path(__file__).resolve()), '--probe'], env=probe_env, check=True, timeout=90)
    print('\n[5/5] Registering Python (SIDM SWAN) for your notebooks ...', flush=True)
    from ipykernel.kernelspec import install
    destination = Path(install(user=True, kernel_name=KERNEL_NAME, display_name='Python (SIDM SWAN)', env=settings))
    env_file = directory / 'environment.sh'
    fd = os.open(env_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        for key, value in settings.items():
            stream.write(f'export {key}={shlex.quote(value)}\n')
    write_private_json(directory / 'ready.json', {'ready': True, 'proxy': str(proxy), 'checked_at': time.time()})
    print(f'SWAN setup passed. Kernel registered in {destination}', flush=True)


def main():
    parser = argparse.ArgumentParser(description='CMS proxy and FNAL access setup for SWAN.')
    parser.add_argument('--use-existing-proxy', action='store_true')
    parser.add_argument('--check-platform', action='store_true')
    parser.add_argument('--probe', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    try:
        if args.check_platform:
            if not is_swan():
                raise RuntimeError('SWAN was not detected. Run this setup from a CERN SWAN terminal.')
            print('SWAN confirmed (CERNBox and SWAN installation markers found).')
        elif args.probe:
            probe_files()
        else:
            setup(args.use_existing_proxy)
    except (RuntimeError, OSError, ValueError, subprocess.SubprocessError) as error:
        print(f'\nSetup did not finish: {error}', file=sys.stderr, flush=True)
        print('Fix the issue above and rerun bash setup_swan.sh. Your notebook analysis has not been run.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
