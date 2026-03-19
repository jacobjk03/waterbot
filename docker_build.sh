#!/bin/bash
set -e

echo "🚀 Building Docker image locally..."

# Check if Docker is running
if ! docker info > /dev/null 2>&1; then
    echo "❌ Docker is not running."
    
    # Detect OS and provide appropriate instructions
    if [[ "$OSTYPE" == "linux-gnu"* ]]; then
        echo "   On Linux, start Docker daemon with:"
        echo "   sudo systemctl start docker"
        echo ""
        echo "   Or if Docker is not installed:"
        echo "   sudo apt-get update && sudo apt-get install -y docker.io"
        echo "   sudo systemctl enable docker"
        echo "   sudo systemctl start docker"
        echo ""
        echo "   Note: You may need to add your user to the docker group:"
        echo "   sudo usermod -aG docker $USER"
        echo "   (Then log out and back in)"
    elif [[ "$OSTYPE" == "darwin"* ]]; then
        echo "   On macOS, start Docker Desktop or Colima:"
    echo "   For Colima: colima start"
        echo "   For Docker Desktop: Open Docker Desktop application"
    else
        echo "   Please ensure Docker is installed and running."
    fi
    exit 1
fi

# Check if user has permission to run Docker (Linux-specific)
if [[ "$OSTYPE" == "linux-gnu"* ]]; then
    if ! docker ps > /dev/null 2>&1; then
        # If running with sudo, try without sudo first
        if [ -n "$SUDO_USER" ]; then
            echo "⚠️  Running with sudo detected."
            echo "   Note: If you add your user to the docker group, you won't need sudo:"
            echo "   sudo usermod -aG docker $SUDO_USER"
            echo "   (Then log out and back in, and run without sudo)"
            echo ""
        else
            echo "⚠️  Docker is running but you may not have permission."
            echo "   Try: sudo docker ps"
            echo "   Or add your user to the docker group:"
            echo "   sudo usermod -aG docker $USER"
            echo "   (Then log out and back in)"
            exit 1
        fi
    fi
fi

# RAG vector store is PostgreSQL (pgvector).
# Chat generation uses Claude; embeddings ingestion may still require OpenAI depending on your setup.
echo "✅ Frontend will be built inside Docker (OS-agnostic)"

# Load environment variables from .env file if it exists (for CLAUDE_API_KEY in container)
if [ -f ".env" ]; then
    echo "📄 Loading environment variables from .env file..."
    export $(grep -v '^#' .env | xargs)
elif [ -f "application/.env" ]; then
    echo "📄 Loading environment variables from application/.env file..."
    export $(grep -v '^#' application/.env | xargs)
fi

# Build arguments (optional - only if CLAUDE_API_KEY is set)
BUILD_ARGS=""
if [ -n "$CLAUDE_API_KEY" ]; then
    BUILD_ARGS="--build-arg CLAUDE_API_KEY=$CLAUDE_API_KEY"
    echo "✅ CLAUDE_API_KEY will be available in container"
fi

# Build the image
# Allow platform override via PLATFORM env var, default to linux/amd64 for consistency
PLATFORM="${PLATFORM:-linux/amd64}"
echo "🔨 Building Docker image for ${PLATFORM} platform..."
echo "   RAG uses PostgreSQL (pgvector); set DATABASE_URL or DB_* in container for RAG."
echo "   Building frontend inside Docker (OS-agnostic)"
docker build --platform ${PLATFORM} $BUILD_ARGS -t waterbot .

echo ""
echo "✅ Build complete!"
echo ""
echo "📦 Image built: waterbot"
echo ""
echo "🚀 To run the container:"
echo "   docker run -p 8000:8000 waterbot"
echo ""
echo "💡 To rebuild components:"
echo "   - Vector database: python application/scripts/Add_files_to_db.py"
echo "   - Frontend is built automatically inside Docker (no manual build needed)"
echo "   Then re-run this script"