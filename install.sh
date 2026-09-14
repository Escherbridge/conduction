#!/usr/bin/env bash
# Install Conduction dependencies
# Idempotent - safe to run multiple times

set -euo pipefail

echo "=== Conduction Installation ==="
echo ""

# Determine script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

# Check if venv exists
if [ ! -d "venv" ]; then
    echo "Creating virtual environment..."

    # Try uv first
    if command -v uv &> /dev/null; then
        echo "  Using uv..."
        uv venv venv
    else
        echo "  Using python -m venv..."
        python3 -m venv venv
    fi
else
    echo "Virtual environment already exists at: $SCRIPT_DIR/venv"
fi

# Activate venv and determine pip command
source venv/bin/activate

if command -v uv &> /dev/null; then
    PIP_CMD="uv pip"
else
    PIP_CMD="pip"
fi

# Install requirements
echo ""
echo "Installing dependencies..."

for req_file in requirements.txt requirements-dev.txt; do
    if [ -f "$req_file" ]; then
        echo "  Installing from $req_file..."
        $PIP_CMD install -r "$req_file"
    else
        echo "  Warning: $req_file not found, skipping"
    fi
done

# Verify installation
echo ""
echo "Verifying installation..."
if python -c "import app; print('OK')" 2>/dev/null; then
    echo "  app module imports successfully"
else
    echo "  Error: Could not import app module"
    exit 1
fi

echo ""
echo "=== Installation Complete ==="
echo ""
echo "Next steps:"
echo "  1. Launch Conduction:"
echo "       ./conduction.sh"
echo ""
