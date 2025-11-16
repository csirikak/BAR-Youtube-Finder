#!/bin/bash

################################################################################
#
#  Removes images from a target directory that are smaller than 1280x720.
#
#  USAGE:
#  1. Save this file as clean_images.sh
#  2. Make it executable:  chmod +x clean_images.sh
#  3. Run a dry run first: ./clean_images.sh
#  4. To delete files:     DRY_RUN=false ./clean_images.sh
#  5. To target a folder:  TARGET_DIR="/path/to/your/images" ./clean_images.sh
#
################################################################################

# --- Configuration ---

# Set to 'false' to actually delete files.
# Set to 'true' to only print what *would* be deleted.
: "${DRY_RUN:=FALSE}"

# The directory to clean. Defaults to the current directory (".").
: "${TARGET_DIR:="."}"

# The minimum dimensions
MIN_WIDTH=1280
MIN_HEIGHT=720

# --- Choose Your Method ---
# METHOD="exiftool"  # Recommended: Fastest, specialized tool
METHOD="exiftool"    # Alternative: Uses ImageMagick, very common

# --- End Configuration ---


# Function to handle the actual deletion or dry-run print
run_delete() {
    if [ "$DRY_RUN" = "true" ]; then
        echo "DRY RUN: Would delete:"
        # Use xargs to print each file on a new line
        xargs -0 -n 1 printf "  - %s\n"
    else
        echo "DELETING files smaller than ${MIN_WIDTH}x${MIN_HEIGHT}..."
        # Use xargs to pass the null-terminated list to rm
        xargs -d '\n' rm --
        echo "Deletion complete."
    fi
}

echo "--- Image Cleaner ---"
echo "Target Directory: $TARGET_DIR"
echo "Minimum Size:     ${MIN_WIDTH}x${MIN_HEIGHT}"
echo "Dry Run:          $DRY_RUN"
echo "Method:           $METHOD"
echo "---------------------"

if [ ! -d "$TARGET_DIR" ]; then
    echo "Error: Target directory '$TARGET_DIR' does not exist."
    exit 1
fi

# Change to the target directory to simplify paths
cd "$TARGET_DIR" || exit 1


if [ "$METHOD" = "exiftool" ]; then
    ### METHOD 1: ExifTool (Fastest)
    # Requires 'exiftool': sudo apt install libimage-exiftool-perl
    if ! command -v exiftool &> /dev/null; then
        echo "Error: 'exiftool' command not found."
        echo "Please install it: sudo apt install libimage-exiftool-perl"
        exit 1
    fi

    echo "Scanning with exiftool..."
    
    # -q: Quiet mode
    # -if: Conditional check on metadata
    # -p '$FileName': Print *only* the filename
    # -print0: Use null characters as a separator (handles all filenames)
    # '.': Scan the current directory (non-recursive)
    exiftool -q -if "\$ImageWidth <= $MIN_WIDTH or \$ImageHeight <= $MIN_HEIGHT" -p '$FileName' . | run_delete

else
    ### METHOD 2: ImageMagick 'identify' (Fast Alternative)
    # Requires 'imagemagick': sudo apt install imagemagick
    if ! command -v identify &> /dev/null; then
        echo "Error: 'identify' command not found."
        echo "Please install it: sudo apt install imagemagick"
        exit 1
    fi

    echo "Scanning with identify (find)..."

    # -maxdepth 1: Prevents recursion. Remove this to scan subfolders.
    # -type f: Only find files.
    # -iname: Case-insensitive extension matching.
    # -exec identify ... {}: Runs identify on batches of files.
    # -format "...": Outputs '1' or '0' (if match) and the filename (%i)
    # \0: Use null char as separator.
    # grep -z -E '^1 ': Filters for lines starting with '1 ' (the matches)
    # cut -z -d' ' -f2-: Removes the '1 ' and keeps the filename.
    find . -maxdepth 1 -type f \( -iname "*.jpg" -o -iname "*.jpeg" -o -iname "*.png" -o -iname "*.gif" -o -iname "*.bmp" -o -iname "*.webp" \) \
        -exec identify -format "%[fx:w<${MIN_WIDTH}||h<${MIN_HEIGHT}] %i\0" {} + | \
        grep -z -E '^1 ' | \
        cut -z -d' ' -f2- | \
        run_delete
fi

echo "---------------------"
echo "Done."