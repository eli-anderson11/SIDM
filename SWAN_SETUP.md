# Using SIDM notebooks on CERN SWAN

For your first setup, complete **Your certificate and proxy** below first. Then open a **Terminal** from the SWAN JupyterLab launcher and change into your SIDM project:

```bash
cd "$CERNBOX_HOME/SWAN_projects/SIDM_abbott"
bash setup_swan.sh
```

The setup runs the usual `setup.sh` first, checks SWAN, prepares CMS authentication, reads five event numbers from a trigger-study file at FNAL, and registers **Python (SIDM SWAN)**. It uses your active virtual environment, or `~/SIDM_env` (creating it when needed). Missing analysis dependencies are installed from the project's `requirements.txt`.

## Your certificate and proxy

A grid certificate identifies you to CMS. A **proxy** is a temporary credential made from that certificate so programs can open files on your behalf. You must have CMS VO membership and permission to read the requested storage; the setup does not grant storage access.

Start with the [US CMS grid certificate instructions](https://www.uscms.org/uscms_at_work/computing/getstarted/get_grid_cert.shtml) and follow **the first three steps**: read the linked TWiki, obtain your password-protected CERN grid certificate, and sign the CMS VO Acceptable Use Policy.

For step 1, in the TWiki section **“Obtaining and Installing your Certificate”**, upload your `.p12` certificate to **your home directory on SWAN (`~`)**. Perform the certificate installation commands in a **SWAN terminal**. Follow the TWiki instructions to create `~/.globus/usercert.pem` and `~/.globus/userkey.pem`, then continue through the proxy creation command:

```bash
voms-proxy-init --rfc --voms cms -valid 192:00
```

If that command succeeds, your certificate and CMS proxy are ready—you are good to go with the SWAN setup. From your SIDM project directory, run `bash setup_swan.sh --use-existing-proxy` to reuse that proxy, check access to a trigger-study file at FNAL, and prepare the notebook kernel.

Keep your certificate files in your SWAN home directory, outside the repository.

Before creating a proxy, set the private key permissions in your SWAN terminal:

```bash
chmod 600 ~/.globus/userkey.pem
```

The VOMS client requires `userkey.pem` to have mode `400` (owner read only) or `600` (owner read/write). This is required by VOMS itself. The setup checks this before requesting your passphrase. These modes apply to the private key; `usercert.pem` does not need the same restriction.

Enter your GRID passphrase directly in the terminal when prompted. Nothing appears while you type. Never put a passphrase, certificate, key, or proxy in a notebook or in Git.

The script requests **192 hours (8 days)** for both the proxy and its CMS VOMS membership. The issuing service can grant less. The script prints both actual lifetimes: renew before the shorter one expires. It does not claim that a request guarantees eight days of access.

If you already have a valid CMS proxy on this SWAN server, you can reuse it without your long-lived certificate/key:

```bash
bash setup_swan.sh --use-existing-proxy
```

The default path is `/tmp/x509up_u$(id -u)`. For a proxy stored somewhere else, set its path before running setup:

```bash
export X509_USER_PROXY=/path/to/your/proxy
chmod 600 "$X509_USER_PROXY"
bash setup_swan.sh --use-existing-proxy
```

Reusing a proxy does **not** extend its lifetime. For normal renewal, run `bash setup_swan.sh` again with your certificate/key available. A SWAN server reset may remove temporary files and require rerunning setup. A notebook kernel restart normally preserves the proxy file.

## Open your notebook

1. Choose **Kernel > Change Kernel > Python (SIDM SWAN)**. If it does not appear yet, refresh JupyterLab.
2. Restart a kernel that was already running, then run the notebook's Setup cells.
3. Expect a message saying that file access uses FNAL EOS via a CMS proxy and showing the remaining hours.

The kernel carries these environment variables into each notebook:

| Variable | Purpose |
| --- | --- |
| `RUNNING_ON_SWAN=true` | Select SWAN file access. |
| `SIDM_SWAN_READY=true` | Setup passed its FNAL read test. The notebook rechecks the setup record and current proxy validity, so this flag alone does not prove continued access. |
| `X509_USER_PROXY` | Location of the shared temporary proxy file. |
| `X509_CERT_DIR` | Directory of trusted grid certificate authorities. |
| `XrdSecPROTOCOL=gsi,unix` | Use the tested FNAL authentication path with Kerberos excluded. |
| `JAVA_HOME` | Java runtime for the SWAN VOMS client, when needed. |

Terminal exports alone do not change notebook kernels. The registered kernel supplies these variables automatically. To also export them in your current terminal, use `source setup_swan.sh` instead of `bash setup_swan.sh` (the same optional argument is supported).

The setup's FNAL read check runs in a fresh process with a nonexistent Kerberos ticket cache. A FNAL Kerberos ticket or CMSLPC login is not needed for the reads.

## Using the same notebook on coffea-casa

Use your usual project setup and normal Python kernel. With `RUNNING_ON_SWAN` unset or false, `configure_notebook()` returns false, and `replace_xcache=running_on_swan` preserves the default coffea-casa endpoint. It does not change coffea-casa authentication.

The compatibility helper changes file access only; it does not change analysis selections, samples, histograms, or plotting code. The environment diagnostic remains in the notebook. Full analysis is separate from the small setup read test.

For maintainers: `python -m unittest discover -s tests -p test_swan.py` runs the offline compatibility checks. VOMS lifetime behavior is described in the [VOMS client manual](https://manpages.debian.org/bookworm/voms-clients-java/voms-proxy-init.1.en.html); kernel environment settings follow the [Jupyter kernel specification](https://jupyter-client.readthedocs.io/en/stable/kernels.html#kernel-specs).
