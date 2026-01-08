#!/usr/bin/env bash
set -euo pipefail

# Usage: ./scripts/release.sh [patch|minor]
# Default: patch

BUMP_TYPE="${1:-patch}"

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

info() { echo -e "${GREEN}[INFO]${NC} $1"; }
warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
error() { echo -e "${RED}[ERROR]${NC} $1"; exit 1; }

# Validate bump type
if [[ "$BUMP_TYPE" != "patch" && "$BUMP_TYPE" != "minor" ]]; then
    error "Invalid bump type: $BUMP_TYPE. Use 'patch' or 'minor'."
fi

info "Bump type: $BUMP_TYPE"

# =============================================================================
# Preflight checks
# =============================================================================

info "Running preflight checks..."

# Check we're in a git repository
if ! git rev-parse --is-inside-work-tree &>/dev/null; then
    error "Not in a git repository"
fi

# Check we're on the default branch (main)
DEFAULT_BRANCH="main"
CURRENT_BRANCH=$(git branch --show-current)
if [[ "$CURRENT_BRANCH" != "$DEFAULT_BRANCH" ]]; then
    error "Not on default branch. Current: $CURRENT_BRANCH, Expected: $DEFAULT_BRANCH"
fi

# Check for uncommitted changes
if ! git diff --quiet || ! git diff --staged --quiet; then
    error "Working directory has uncommitted changes. Commit or stash them first."
fi

# Check for untracked files (warning only)
UNTRACKED=$(git ls-files --others --exclude-standard)
if [[ -n "$UNTRACKED" ]]; then
    warn "Untracked files present (continuing anyway):"
    echo "$UNTRACKED" | head -5
fi

# Check that gh CLI is installed and authenticated (warn only, we have a fallback)
GH_AVAILABLE=true
if ! command -v gh &>/dev/null; then
    warn "GitHub CLI (gh) is not installed. You'll need to create the release manually."
    GH_AVAILABLE=false
elif ! gh auth status &>/dev/null; then
    warn "GitHub CLI is not authenticated. You'll need to create the release manually."
    GH_AVAILABLE=false
fi

# Fetch latest from remote
info "Fetching latest from origin..."
git fetch origin "$DEFAULT_BRANCH" --tags

# Check if local branch is up to date with remote
LOCAL_COMMIT=$(git rev-parse HEAD)
REMOTE_COMMIT=$(git rev-parse "origin/$DEFAULT_BRANCH")
if [[ "$LOCAL_COMMIT" != "$REMOTE_COMMIT" ]]; then
    error "Local branch is not up to date with origin/$DEFAULT_BRANCH. Run 'git pull' first."
fi

# Check if there are commits since last tag
LAST_TAG=$(git describe --tags --abbrev=0 2>/dev/null || echo "")
if [[ -n "$LAST_TAG" ]]; then
    COMMITS_SINCE_TAG=$(git rev-list "$LAST_TAG"..HEAD --count)
    if [[ "$COMMITS_SINCE_TAG" -eq 0 ]]; then
        error "No commits since last tag ($LAST_TAG). Nothing to release."
    fi
    info "Commits since $LAST_TAG: $COMMITS_SINCE_TAG"
fi

# Check that lint passes
info "Running linter..."
if ! uv run ruff check drift/ tests/; then
    error "Lint failed. Fix issues before releasing."
fi

# Check formatting
info "Checking formatting..."
if ! uv run ruff format --check drift/ tests/; then
    error "Format check failed. Run 'uv run ruff format drift/ tests/' first."
fi

# Check that tests pass
info "Running tests..."
if ! uv run pytest tests/unit/ -v; then
    error "Tests failed. Fix them before releasing."
fi

info "✓ All preflight checks passed"

# =============================================================================
# Calculate new version
# =============================================================================

# Get current version from pyproject.toml
CURRENT_VERSION=$(grep -E '^version = "' pyproject.toml | sed -E 's/version = "(.*)"/\1/')
if [[ -z "$CURRENT_VERSION" ]]; then
    error "Failed to read version from pyproject.toml"
fi

info "Current version: $CURRENT_VERSION"

# Parse version
IFS='.' read -r MAJOR MINOR PATCH <<< "$CURRENT_VERSION"

# Validate parsed version
if [[ -z "$MAJOR" || -z "$MINOR" || -z "$PATCH" ]]; then
    error "Failed to parse version: $CURRENT_VERSION"
fi

# Increment based on bump type
case "$BUMP_TYPE" in
    patch)
        PATCH=$((PATCH + 1))
        ;;
    minor)
        MINOR=$((MINOR + 1))
        PATCH=0
        ;;
esac

NEW_VERSION="${MAJOR}.${MINOR}.${PATCH}"
NEW_TAG="v${NEW_VERSION}"

info "Version bump: $CURRENT_VERSION → $NEW_VERSION"

# Check if tag already exists
if git rev-parse "$NEW_TAG" &>/dev/null; then
    error "Tag $NEW_TAG already exists!"
fi

# =============================================================================
# Confirm and create release
# =============================================================================

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Ready to release: $NEW_VERSION"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "This will:"
echo "  1. Update version in pyproject.toml to $NEW_VERSION"
echo "  2. Commit the version bump"
echo "  3. Create and push tag $NEW_TAG"
echo "  4. Create a GitHub Release (triggers PyPI publish)"
echo ""

read -p "Proceed? [y/N] " -n 1 -r
echo ""

if [[ ! $REPLY =~ ^[Yy]$ ]]; then
    info "Aborted."
    exit 0
fi

# Update version in pyproject.toml
info "Updating pyproject.toml..."
if [[ "$OSTYPE" == "darwin"* ]]; then
    # macOS
    sed -i '' "s/^version = \".*\"/version = \"$NEW_VERSION\"/" pyproject.toml
else
    # Linux
    sed -i "s/^version = \".*\"/version = \"$NEW_VERSION\"/" pyproject.toml
fi

# Verify the change
UPDATED_VERSION=$(grep -E '^version = "' pyproject.toml | sed -E 's/version = "(.*)"/\1/')
if [[ "$UPDATED_VERSION" != "$NEW_VERSION" ]]; then
    error "Failed to update version in pyproject.toml"
fi

# Commit version bump
info "Committing version bump..."
git add pyproject.toml
git commit -m "chore: bump version to $NEW_VERSION"

# Create annotated tag
info "Creating tag $NEW_TAG..."
git tag -a "$NEW_TAG" -m "Release $NEW_VERSION"

# Push commit and tag to origin
info "Pushing to origin..."
git push origin "$DEFAULT_BRANCH"
git push origin "$NEW_TAG"

# Create GitHub Release (triggers the publish workflow)
if [[ "$GH_AVAILABLE" == "true" ]]; then
    info "Creating GitHub Release..."
    if gh release create "$NEW_TAG" --generate-notes --title "$NEW_TAG"; then
        echo ""
        info "✓ Released $NEW_VERSION"
        info "GitHub Actions will now build and publish to PyPI."
        info "Watch progress at: https://github.com/Use-Tusk/drift-python-sdk/actions"
    else
        echo ""
        warn "Failed to create GitHub Release via CLI."
        warn "The tag $NEW_TAG has been pushed. To trigger the PyPI publish:"
        echo ""
        echo "  1. Go to: https://github.com/Use-Tusk/drift-python-sdk/releases/new"
        echo "  2. Select the tag: $NEW_TAG"
        echo "  3. Set the title: $NEW_TAG"
        echo "  4. Click 'Generate release notes' (optional)"
        echo "  5. Click 'Publish release'"
        echo ""
    fi
else
    echo ""
    info "✓ Tag $NEW_TAG pushed successfully."
    info "To publish to PyPI, create a GitHub Release:"
    echo ""
    echo "  1. Go to: https://github.com/Use-Tusk/drift-python-sdk/releases/new"
    echo "  2. Select the tag: $NEW_TAG"
    echo "  3. Set the title: Release $NEW_VERSION"
    echo "  4. Click 'Generate release notes' (optional)"
    echo "  5. Click 'Publish release'"
    echo ""
fi
