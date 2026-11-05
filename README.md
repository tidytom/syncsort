# SyncSort
A desktop app for Windows (and Linux) that finds duplicate files, compares two folders, and helps you clean up safely, for anyone with messy photo, document or backup folders.

> Screenshot: add `docs/screenshot.png` (placeholder line — leave it exactly like this for GUI apps)

## What it does
- Finds duplicate files inside one folder, or files in a "cleanup" folder that already exist in a "master" folder.
- Lists files that are only in the cleanup folder, and files that are only in the master folder.
- Lets you choose which copy of a duplicate to keep, then sends the other copies to the Recycle Bin after a byte-for-byte check.
- Copies or moves whole folders, unzips many zip files at once (including Google Takeout), and removes empty folders.
- Finds Google Drive shortcut files (.gdoc, .gsheet and similar) and copies or moves them to another folder.

## What you need
- Windows 10 or 11 is best. Linux also works (the Robocopy, Windows tar and Explorer options are Windows only).
- Python 3.9 or newer, with Tkinter (see the table below).
- An internet connection the first time you run it, so it can download its helper packages.
- Optional: **ffprobe** (part of FFmpeg) on your `PATH`, to show video resolutions. Without it, the video resolution column stays blank.

### Tkinter (cannot be installed with pip)
The window is built with Tkinter. It comes with the main Python install, not with pip, so make sure your Python has it:

| Platform         | How to get Tkinter                                          |
|------------------|-------------------------------------------------------------|
| Windows          | Install Python from python.org with the **tcl/tk** option (on by default) |
| Debian / Ubuntu  | `sudo apt install python3-tk`                               |
| Fedora           | `sudo dnf install python3-tkinter`                          |

## Install
1. Download this repository (green **Code** button, then **Download ZIP**) and unzip it.
2. Open a terminal in the folder and run:
   ```bash
   python syncsort.py
   ```
3. On the first launch the program sets itself up:
   - it creates a private virtual environment named `.venv` next to the script,
   - it installs the packages it needs (`send2trash`, `Pillow`, `pymupdf`) into that `.venv` only, never into your main Python,
   - it then restarts itself using the `.venv`.
4. Later launches reuse the `.venv` and start straight away. The install step runs again only if the package list changes.

Options:
- **Skip the automatic setup:** set the environment variable `SYNCSORT_NO_BOOTSTRAP=1` and install the packages yourself with `pip install -r requirements.txt`.
- **Rebuild the setup:** delete the `.venv` folder and run the program again.

### In VS Code (Windows or Linux)
1. Open this folder in VS Code with the **Python** extension installed.
2. Run the script once (`python syncsort.py` in the terminal, or press **F5** and pick **Run SyncSort**) so the `.venv` folder is created.
3. VS Code finds the `.venv` folder and uses it, so Pillow and pymupdf are found and the "import could not be resolved" warnings go away. If it does not switch by itself, press **Ctrl+Shift+P**, choose **Python: Select Interpreter**, and pick the one under `.venv`.

The included `.vscode/settings.json` and `.vscode/launch.json` set this up on both Windows and Linux.

## How to use

### Find duplicates in one folder
1. Under **Mode**, choose **Single Folder Cleanup**.
2. Next to **2. CLEANUP Folder**, click **Browse...** and pick the folder.
3. Optional: under **Protected Folders (never deleted)**, click **Add...** for any folder that must never lose a file.
4. Pick a **Match Criteria** and a **Scan Depth** (see Settings below), then click **START SCAN**.
5. Open tab **4. Internal Duplicates**. Each group shows every copy of one file. The first row of a group, marked **KEEP**, is the copy that stays. Click a row to see a preview on the right.
6. To keep a different copy, click its row and click **Keep This Copy**. It moves to the top of the group and becomes the **KEEP** row.
7. Choose what to remove:
   - **Select All Except Best** selects every copy except the **KEEP** row of each group.
   - **Select Duplicates in This Folder...** opens a list of folders. Pick one and click **Select Duplicates**. Every copy in that folder is selected, except the **KEEP** row of each group, so one copy of each file always stays.
   - Or click rows yourself (hold **Ctrl** or **Shift** to pick several).
8. Click **Trash Selected** to send the selected copies to the Recycle Bin, or **Move Selected to Folder...** to move them somewhere else. **Auto-Keep Best Path** does the whole job in one step: it keeps the **KEEP** row of every group and trashes the rest.

### Compare two folders
1. Under **Mode**, choose **Compare Two Folders**.
2. Set **1. PROTECTED Folder** (the master, the copy you trust) and **2. CLEANUP Folder** (the folder to tidy). They must be two separate folders; one cannot be inside the other.
3. Click **START SCAN**. Then use the tabs:
   - **1. Safe to Delete (Matches Master)**: files in the cleanup folder that already exist in the master. Click **Trash ALL listed here**, or select some and click **Move Selected to Folder...**.
   - **2. Only in Target (Not in Master)**: files in the cleanup folder with no match in the master, grouped by folder. **Copy These to Master** copies them into the master, keeping their subfolders. **Export List...** saves the list as a text file.
   - **3. Only in Master**: files in the master with no match in the cleanup folder, grouped by folder. This list is read-only. **Export List...** saves it as a text file.
   - **4. Internal Duplicates**: duplicates inside the cleanup folder, as described above.
4. Double-click any file row to open its folder in Explorer.

### Other tabs
- **5. Copy / Move Folder**: pick a **Source Folder** and a **Destination Folder**, choose **Copy (keep source)** or **Move (delete source after)**, then click **START**. **CANCEL** stops it.
- **6. Unzip**: click **Add Zip Files...** or **Add Folder of Zips...**, set the options, then click **UNZIP ALL**. **Merge all into one folder (Takeout mode)** puts every zip into one `Takeout_Merged` folder next to the first zip.
- **7. Empty Folders**: pick a **Folder to scan**, click **SCAN**, select folders (or click **SELECT ALL**) and click **DELETE SELECTED**.
- **8. Google Drive Shortcuts**: pick a **Folder to scan** and click **SCAN** to list every `.gdoc`, `.gsheet`, `.gslides`, `.gdraw`, `.gform`, `.gmap`, `.gsite`, `.gjam` and `.glink` file in it and its subfolders. Choose **Copy** or **Move**, tick or untick **Keep folder structure**, then click **Copy / Move to Folder...** and pick a destination. It uses the selected rows, or every listed file if none are selected.
- **9. Log**: everything the program did, including any files it could not read. **Clear Log** empties it.

### Save a scan for later
Use the **Cache / Save** menu: **Save Scan Results to File...** and **Load Scan Results from File...**. A loaded scan keeps its master folder and protected folders. **Clear Current Results** empties the lists.

## Settings and options
- **Match Criteria**
  - **Exact File Bytes (Hash)**: files must have exactly the same content. This is the default and the safest.
  - **Same Name & Size**: fast, matches files with the same name and size.
  - **Same Text Content (PDFs/Text)**: matches PDFs and text files that contain the same text, even if the files differ.
- **Scan Depth** (only for Exact File Bytes)
  - **Smart (recommended)**: reads the start and end of each file first, then fully checks only the likely matches.
  - **Full (hash everything)**: reads every file completely. Slower.
- **Permanently delete (skip Recycle Bin — no undo)**: off by default. When on, files are deleted instead of going to the Recycle Bin.
- **Laptop mode (fewer threads, less heat)**: uses fewer threads and larger (256 KB) reads, so a laptop runs cooler and quieter. The Copy tab's "Auto" thread count is lowered too. Saved scans work in either mode.
- **Copy / Move Folder tab**: **Engine** (Robocopy for Windows, or Python for any system), **Threads**, and **Replace files that already exist in the destination** (off by default).
- **Unzip tab**: **Engine** (**Windows tar (recommended)** or **Explorer (small zips only)**), **Overwrite existing files**, **Extract into subfolder (zip name)**, **Merge all into one folder (Takeout mode)**.
- **Google Drive Shortcuts tab**: **Copy (safe, originals stay)** is the default. **Move (removes them from the scanned folder)** and **Keep folder structure** (on by default).

## Safety notes
- Before any file is deleted, it is compared byte-for-byte with the copy being kept, whichever Match Criteria you chose. A file that is not identical is kept.
- The copy being kept (the **KEEP** row of each group in tab 4) is never trashed and never moved, even if you select it or its whole group.
- The duplicate tabs (1 to 4) never delete or move anything in the master folder or a protected folder, including after loading a saved scan. Tab 5 (Copy / Move Folder) does not check protected folders, so choose its source folder with care.
- A file is never treated as its own duplicate. Linked folders (shortcuts, junctions and symlinks) are not followed, and the same file reached two ways is skipped.
- Deleted files go to the Recycle Bin unless you tick **Permanently delete**. To undo, restore them from the Recycle Bin.
- After deleting or moving, empty folders are removed only in the cleanup folder, and only if you click **Yes**.
- **Move Selected to Folder...** (tabs 1, 2 and 4) and the Google Drive tab never replace a file at the destination. A clashing file gets a new name such as "photo (moved 1).jpg" or "budget (copied 1).gsheet".
- The Copy / Move tab skips files that already exist in the destination unless you tick **Replace files that already exist in the destination**.
- **Google Drive warning:** `.gdoc`, `.gsheet` and similar files are links to documents stored in Google Drive. Moving them out of a folder that Google Drive syncs can remove the documents from Google Drive. Use **Copy** unless you are sure. In Move mode, files in protected folders are skipped.
- **DELETE SELECTED** in the Empty Folders tab deletes folders permanently (they are empty, so no files are lost). Folders holding only hidden system files such as Thumbs.db or desktop.ini count as empty.
- The program does not send anything over the internet. It only downloads its helper packages on the first launch.

## Troubleshooting
- **"Tkinter is not available"**: install Tkinter for your system (see the table above), delete the `.venv` folder, and run the program again.
- **"Some files were not scanned"**: those files were locked or access was denied. They are listed in tab **9. Log**. Close the programs using them and scan again.
- **Nothing was deleted**: the files were not byte-for-byte identical to the kept copy, so they were kept on purpose. The Log tab lists each one.
- **An empty folder will not delete**: close any Explorer window that is open in it, pause Google Drive or OneDrive sync, then scan again.
- **Video resolutions are blank**: install FFmpeg and make sure `ffprobe` is on your `PATH`.

## How it works
SyncSort is a single Python file with a Tkinter interface. A scan first lists every file with its size; for exact matching it skips files whose size is unique (they cannot have an exact duplicate) and hashes the rest with MD5 in a thread pool; in Smart mode it hashes the first and last 64 KB and then fully hashes only the candidates. Every delete runs a final byte-for-byte comparison in a background thread, and all window updates go through `root.after` so the interface stays responsive. Files go to the Recycle Bin through `send2trash`, image previews use Pillow, and PDF text matching uses PyMuPDF.

## License

MIT. See [LICENSE](LICENSE).
