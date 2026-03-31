"""
Passivbot Deployment Script

Deploys passivbot to remote VPS using scp with tar compression over SSH.
Cross-platform compatible (Windows, Linux, macOS).

Deployment method:
- Creates compressed tar.gz archive locally (with exclusions)
- Transfers single archive file via scp (efficient single-file transfer)
- Extracts archive on remote server
- Falls back to rsync if available (faster incremental updates)

What gets deployed:
- All Python source code (src/, *.py)
- Rust source code (passivbot-rust/src/, Cargo.toml, Cargo.lock)
- Configuration examples and documentation
- Docker assets (Dockerfile, docker-compose.yml)
- Requirements files
- Configs directory
- Scripts directory

What gets excluded:
- Git files (.git/, .gitignore)
- Virtual environments (venv/, __pycache__/)
- Build artifacts (*.pyc, build/, dist/, passivbot-rust/target/)
- Rust compiled files (*.so, *.pyd, *.dylib, .compile.lock)
- Log files (*.log, logs/)
- SSH keys (*.pem)
- Historical data and caches (historical_data/, caches/, backtests/)
  * EXCEPTION: historical_data/ohlcvs_hyperliquid/HYPE is always deployed
- Test files (tests/, pytest.ini)
- Optimize results (optimize_results/)
- IDE settings (.vscode/, .idea/, .claude/)
- Temporary files (*.swp, *.swo, .DS_Store)

OPTIONAL: api-keys.json deployment
- By default, api-keys.json is NOT deployed (contains sensitive credentials)
- Use --with-api-keys flag to deploy it (NOT recommended for production)
- Best practice: Create api-keys.json manually on remote server

CRITICAL: After deployment, you MUST:
1. Create or verify api-keys.json with your API credentials on remote server
2. Create configs with your bot parameters
3. Install dependencies: pip install -r requirements.txt
4. Or use Docker: docker compose build && docker compose up -d

Usage:
    python deploy_passivbot.py              # Deploy without api-keys.json
    python deploy_passivbot.py --with-api-keys  # Deploy with api-keys.json (use with caution)
"""

import os
import sys
import subprocess
import platform
from pathlib import Path

# Configuration
REMOTE_USER = "ubuntu"
REMOTE_HOST = "54.95.246.213"
REMOTE_PATH = "/home/ubuntu/passivbot"
LOCAL_PATH = "."
SSH_KEY_NAME = "lighter.pem"  # SSH key filename

# Parse command line arguments
DEPLOY_API_KEYS = "--with-api-keys" in sys.argv

# Find SSH key in multiple locations (cross-platform: Windows + WSL)
def find_ssh_key():
    """Find SSH key in Windows or WSL filesystem."""
    possible_paths = [
        os.path.expanduser(f"~/{SSH_KEY_NAME}"),  # WSL/Linux home directory
        f"./{SSH_KEY_NAME}",  # Current directory (Windows native)
        os.path.join(os.getcwd(), SSH_KEY_NAME),  # Absolute current dir
        SSH_KEY_NAME,  # Relative path
    ]

    for path in possible_paths:
        if os.path.exists(path):
            return os.path.abspath(path)

    return None

SSH_KEY = find_ssh_key()

# Files that must always be shipped even if they match an exclusion rule
INCLUDE_ALWAYS = {
    "Dockerfile",
    "docker-compose.yml",
    "requirements.txt",
    "requirements-live.txt",
    "requirements-rust.txt",
    "setup.py",
    "Cargo.toml",
    "Cargo.lock",
}

# Files and directories to exclude from deployment
EXCLUDE_PATTERNS = [
    '.git/',
    '.gitignore',
    '.github/',
    'venv/',
    '__pycache__/',
    '*.pyc',
    '*.pyo',
    '*.pyd',
    '.Python',
    'build/',
    'develop-eggs/',
    'dist/',
    'downloads/',
    'eggs/',
    '.eggs/',
    'lib/',
    'lib64/',
    'parts/',
    'sdist/',
    'var/',
    'wheels/',
    '*.egg-info/',
    '.installed.cfg',
    '*.egg',
    '*.log',
    'logs/',
    'logs_remote/',
    'historical_data/',  # Large data files - download separately on remote
    'caches/',  # Cache files - will be regenerated
    'backtests/',  # Backtest results - not needed on remote
    'optimize_results/',  # Optimization results - not needed on remote
    'notebooks/',  # Jupyter notebooks - development only
    'tests/',  # Test files - not needed in production
    'pytest.ini',
    '.vscode/',
    '.idea/',
    '.claude/',  # Claude Code settings
    '*.pem',  # SSH keys
    'lighter.pem',
    'api-keys.json',  # NEVER deploy by default - create manually on remote server
    'nul',  # Windows null device file
    '*.swp',  # Vim swap files
    '*.swo',
    '.DS_Store',  # macOS
    'Thumbs.db',  # Windows
    'deploy.py',  # Don't deploy deployment scripts
    'deploy_passivbot.py',
    '.prospector.yml',  # Development tools
    '.readthedocs.yaml',
    'other_docs/',  # Documentation - not needed for running bot
    'docs/',
    'mkdocs.yml',
    # Rust build artifacts and temporary files (but include source code)
    'passivbot-rust/target/',  # Rust compiled binaries - will be built on remote
    'passivbot-rust/.compile.lock',  # Temporary lock file
    'passivbot-rust/**/*.so',  # Compiled shared libraries
    'passivbot-rust/**/*.pyd',  # Python compiled extensions
    'passivbot-rust/**/*.dylib',  # macOS dynamic libraries
]

# Override: include api-keys.json if flag is set
if DEPLOY_API_KEYS:
    EXCLUDE_PATTERNS = [p for p in EXCLUDE_PATTERNS if p != 'api-keys.json']
    print("\n" + "!" * 60)
    print("WARNING: Deploying api-keys.json with credentials!".center(60))
    print("!" * 60 + "\n")


def print_header(text):
    """Print a formatted header."""
    print("\n" + "=" * 60)
    print(text.center(60))
    print("=" * 60 + "\n")


def print_success(text):
    """Print success message."""
    print(f"[OK] {text}")


def print_error(text):
    """Print error message."""
    print(f"[ERROR] {text}")


def print_warning(text):
    """Print warning message."""
    print(f"[WARNING] {text}")


def print_info(text):
    """Print info message."""
    print(f"[INFO] {text}")


def check_ssh_key():
    """Check if SSH key exists and set proper permissions."""
    if SSH_KEY is None:
        print_error(f"SSH key '{SSH_KEY_NAME}' not found in any expected location")
        print("Searched locations:")
        print(f"  - ~/{SSH_KEY_NAME} (WSL/Linux home)")
        print(f"  - ./{SSH_KEY_NAME} (current directory)")
        print(f"Please ensure {SSH_KEY_NAME} is in one of these locations")
        return False

    ssh_key_path = Path(SSH_KEY)
    print_success(f"Found SSH key: {SSH_KEY}")

    # Set proper permissions (Unix-like systems only)
    if platform.system() != "Windows":
        try:
            os.chmod(ssh_key_path, 0o600)
            print_success(f"SSH key permissions set to 600")
        except Exception as e:
            print_warning(f"Could not set SSH key permissions: {e}")

    return True


def test_ssh_connection():
    """Test SSH connection to remote server."""
    print_info("Testing SSH connection (may take up to 60 seconds)...")

    cmd = [
        "ssh",
        "-i", SSH_KEY,
        "-o", "ConnectTimeout=30",
        "-o", "StrictHostKeyChecking=no",
        f"{REMOTE_USER}@{REMOTE_HOST}",
        "echo 'Connection successful'"
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60
        )

        if result.returncode == 0:
            print_success("SSH connection successful")
            return True
        else:
            print_error("Cannot connect to remote server")
            print(f"Host: {REMOTE_USER}@{REMOTE_HOST}")
            print(f"Key: {SSH_KEY}")
            if result.stderr:
                print(f"Error: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print_error("SSH connection timed out after 60 seconds")
        return False
    except FileNotFoundError:
        print_error("SSH client not found. Please install OpenSSH.")
        return False
    except Exception as e:
        print_error(f"SSH test failed: {e}")
        return False


def create_remote_directory():
    """Create remote directory if it doesn't exist."""
    print_info("Ensuring remote directory exists...")

    cmd = [
        "ssh",
        "-i", SSH_KEY,
        "-o", "ConnectTimeout=30",
        "-o", "StrictHostKeyChecking=no",
        f"{REMOTE_USER}@{REMOTE_HOST}",
        f"mkdir -p {REMOTE_PATH}"
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=60
        )
        if result.returncode == 0:
            print_success(f"Remote directory ready: {REMOTE_PATH}")
            return True
        else:
            print_error(f"Failed to create remote directory: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print_error("Remote directory creation timed out after 60 seconds")
        return False
    except Exception as e:
        print_error(f"Error creating remote directory: {e}")
        return False


def check_rsync_available():
    """Check if rsync is available (disabled on Windows)."""
    # Disable rsync on Windows - even if available, it doesn't work reliably
    if platform.system() == "Windows":
        return False

    try:
        result = subprocess.run(
            ["rsync", "--version"],
            capture_output=True,
            text=True
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def deploy_with_rsync():
    """Deploy using rsync (faster, incremental)."""
    print_info("Deploying using rsync (incremental sync)...")

    # Build exclude arguments
    exclude_args = []
    for pattern in EXCLUDE_PATTERNS:
        exclude_args.extend(["--exclude", pattern])

    cmd = [
        "rsync",
        "-avz",
        "--progress",
        "--delete",
        "-e", f"ssh -i {SSH_KEY} -o StrictHostKeyChecking=no -o ConnectTimeout=30",
        *exclude_args,
        f"{LOCAL_PATH}/",
        f"{REMOTE_USER}@{REMOTE_HOST}:{REMOTE_PATH}/"
    ]

    try:
        result = subprocess.run(cmd, timeout=300)
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        print_error("Rsync deployment timed out after 5 minutes")
        return False
    except Exception as e:
        print_error(f"Rsync deployment failed: {e}")
        return False


def deploy_with_scp():
    """Deploy using scp with tar compression (efficient single-file transfer)."""
    print_info("Deploying using scp with tar compression...")

    import tempfile
    import tarfile

    # Create temporary tar file
    with tempfile.NamedTemporaryFile(suffix='.tar.gz', delete=False) as tmp_tar:
        tar_path = tmp_tar.name

    try:
        print_info("Creating compressed archive...")

        # Create tar archive with exclusions
        def filter_exclude(tarinfo):
            """Filter function to exclude files matching patterns."""
            # Get relative path
            path_str = tarinfo.name
            base_name = os.path.basename(path_str)

            # Never exclude files explicitly marked for inclusion
            if base_name in INCLUDE_ALWAYS:
                return tarinfo

            # Include historical_data/ohlcvs_hyperliquid/HYPE specifically
            if 'ohlcvs_hyperliquid/HYPE' in path_str or 'ohlcvs_hyperliquid\\HYPE' in path_str:
                return tarinfo

            for pattern in EXCLUDE_PATTERNS:
                if pattern.endswith('/'):
                    # Directory pattern - check if directory name matches
                    dir_name = pattern.rstrip('/')
                    if dir_name in path_str.split('/'):
                        # Special case: allow traversal of historical_data to reach HYPE
                        if dir_name == 'historical_data':
                            # Check if this is on the path to HYPE
                            if path_str == 'passivbot/historical_data' or \
                               path_str == 'passivbot/historical_data/ohlcvs_hyperliquid' or \
                               'ohlcvs_hyperliquid/HYPE' in path_str or \
                               'ohlcvs_hyperliquid\\HYPE' in path_str:
                                continue  # Don't exclude, continue checking other patterns
                        return None
                elif '*' in pattern:
                    # Wildcard pattern
                    import fnmatch
                    if fnmatch.fnmatch(path_str, pattern) or fnmatch.fnmatch(base_name, pattern):
                        return None
                else:
                    # Exact filename match
                    if pattern == base_name or pattern in path_str:
                        return None

            return tarinfo

        # Create the archive
        with tarfile.open(tar_path, 'w:gz') as tar:
            tar.add(LOCAL_PATH, arcname='passivbot', filter=filter_exclude)

        # Get archive size
        archive_size_mb = os.path.getsize(tar_path) / (1024 * 1024)
        print_success(f"Archive created: {archive_size_mb:.2f} MB")

        # Transfer archive to remote
        print_info("Transferring archive to remote server...")

        scp_cmd = [
            "scp",
            "-i", SSH_KEY,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=30",
            tar_path,
            f"{REMOTE_USER}@{REMOTE_HOST}:/tmp/passivbot_deploy.tar.gz"
        ]

        result = subprocess.run(scp_cmd, capture_output=True, text=True, timeout=120)
        if result.returncode != 0:
            print_error(f"Failed to transfer archive: {result.stderr}")
            return False

        print_success("Archive transferred successfully")

        # Extract archive on remote server
        print_info("Extracting archive on remote server...")

        extract_cmd = [
            "ssh",
            "-i", SSH_KEY,
            "-o", "StrictHostKeyChecking=no",
            "-o", "ConnectTimeout=30",
            f"{REMOTE_USER}@{REMOTE_HOST}",
            f"cd {REMOTE_PATH} && tar -xzf /tmp/passivbot_deploy.tar.gz --strip-components=1 && rm /tmp/passivbot_deploy.tar.gz"
        ]

        result = subprocess.run(extract_cmd, capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            print_error(f"Failed to extract archive: {result.stderr}")
            return False

        print_success("Deployment completed successfully")
        return True

    except Exception as e:
        print_error(f"SCP deployment failed: {e}")
        return False
    finally:
        # Clean up local tar file
        try:
            if os.path.exists(tar_path):
                os.unlink(tar_path)
        except:
            pass


def display_next_steps():
    """Display next steps after successful deployment."""
    print("\n" + "=" * 60)
    print("Deployment completed successfully!".center(60))
    print("=" * 60 + "\n")

    print("Next steps:\n")
    print(f"1. SSH into the server:")
    print(f"   ssh -i {SSH_KEY} {REMOTE_USER}@{REMOTE_HOST}\n")

    print(f"2. Navigate to the passivbot directory:")
    print(f"   cd {REMOTE_PATH}\n")

    if not DEPLOY_API_KEYS:
        print("3. Create api-keys.json with your API credentials:")
        print("   nano api-keys.json")
        print("   (Copy contents from your local api-keys.json file)")
        print("   See api-keys.json.example for format\n")
    else:
        print("3. Verify api-keys.json was deployed:")
        print("   cat api-keys.json")
        print("   (Ensure all credentials are correct)\n")

    print("4. Create or modify configs for your trading strategy:")
    print("   ls configs/")
    print("   nano configs/your_config.json\n")

    print("5. Install dependencies:")
    print("   Option A - Using Docker (recommended):")
    print("     docker compose build")
    print("     docker compose up -d")
    print("     docker compose logs -f")
    print("   Option B - Using Python directly:")
    print("     python3 -m venv venv")
    print("     source venv/bin/activate")
    print("     pip install -r requirements.txt")
    print("     # Optional: Build Rust optimization module for faster backtesting")
    print("     cd passivbot-rust && cargo build --release && cd ..\n")

    print("6. Run the bot:")
    print("   Option A - Docker:")
    print("     docker compose up -d")
    print("   Option B - Python:")
    print("     python src/main.py [arguments]\n")

    print("7. Monitor the bot:")
    print("   docker compose logs -f  # if using Docker")
    print("   tail -f logs/passivbot.log  # if using Python\n")

    print("=" * 60)
    print("IMPORTANT NOTES:")
    print("=" * 60)
    if not DEPLOY_API_KEYS:
        print("• api-keys.json was NOT deployed - create it manually")
    else:
        print("• api-keys.json WAS deployed - verify credentials are correct")
    print("• Rust source code deployed - will be compiled on remote if needed")
    print("• Rust target/ (compiled files) NOT deployed - builds fresh on remote")
    print("• Historical data not deployed - download on remote if needed")
    print("• Use configs directory for bot configuration files")
    print("• Always test with small amounts first!")
    print("• Monitor bot logs regularly")


def main():
    """Main deployment function."""
    print_header("Passivbot - Remote Deployment Script")

    # Step 1: Check SSH key
    if not check_ssh_key():
        sys.exit(1)

    # Step 2: Test SSH connection
    if not test_ssh_connection():
        sys.exit(1)

    # Step 3: Create remote directory
    if not create_remote_directory():
        sys.exit(1)

    # Step 4: Display deployment details
    print("\nDeployment Details:")
    print(f"  Local path:  {LOCAL_PATH}")
    print(f"  Remote host: {REMOTE_USER}@{REMOTE_HOST}")
    print(f"  Remote path: {REMOTE_PATH}")
    print(f"  SSH key:     {SSH_KEY}")
    print()

    # Check if api-keys.json exists locally
    api_keys_file = Path("api-keys.json")

    if api_keys_file.exists():
        if DEPLOY_API_KEYS:
            print_warning("api-keys.json WILL BE DEPLOYED (--with-api-keys flag used)")
            print_warning("This file contains sensitive credentials!")
        else:
            print_warning("api-keys.json will NOT be deployed (security best practice)")
            print_info("You must create api-keys.json manually on the remote server")
            print_info("Use --with-api-keys flag to deploy it (not recommended)")
    else:
        print_warning("api-keys.json not found locally")
        print_info("Remember to create api-keys.json on the remote server")
        print_info("See api-keys.json.example for the required format")
    print()

    # Step 5: Deploy (auto-confirmed)
    success = False

    # Use scp with tar compression (efficient single-file transfer)
    # Falls back to rsync if available for incremental updates
    if check_rsync_available():
        print_info("rsync detected - using for incremental sync")
        success = deploy_with_rsync()
    else:
        success = deploy_with_scp()

    # Step 6: Display results
    if success:
        display_next_steps()
    else:
        print("\n" + "=" * 60)
        print("Deployment failed!".center(60))
        print("=" * 60 + "\n")
        sys.exit(1)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n")
        print_warning("Deployment interrupted by user")
        sys.exit(1)
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
