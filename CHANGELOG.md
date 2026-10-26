# Changelog

## 1.3.1 — 2026-10-26

- Fixed **Sort by faces** freezing ("Not responding") after the face search finished. Grouping the faces
  into people ran inside the window and slowed down sharply with big collections (35 s for 19,000 faces,
  many minutes or out of memory beyond that). It now runs in the background with a progress bar and
  takes under a second for 19,000 faces, about 10 s for 60,000.
- Face search is faster: it uses several processor cores, reads photos at a smaller size, skips tiny
  background faces and recognises at most the 24 biggest faces in a crowd photo.
- Results are saved as it goes: pressing **Find faces** again on the same photos is instant, and after
  **Stop** (or closing FileHunter) it carries on where it left off. Edited photos are looked at again.
- The people list shows the first 300 people (everyone is still sorted), so it opens quickly.

## 1.3.0 — 2026-10-25

- New **Sort by faces…**: finds the faces in your photos, groups them into people, and makes a folder
  for each person. Name the people and put them in priority order: a group photo goes to the person
  highest in the list (or, if you prefer, a copy goes to everyone in it). Photos with nobody from the
  list can go to an "Others" folder.
- See each person's faces and untick wrong ones, merge two groups that are the same person, or leave
  someone out. Names and order are remembered for next time.
- Runs entirely on your computer with OpenCV's free YuNet and SFace models (downloaded once, ~39 MB).

## 1.2.0 — 2026-10-25

- New **Report** after every copy or move: lists each file that was not copied (failed, skipped,
  already there, or not reached because you pressed Stop) with the reason in plain words —
  "the file is open in another program", "the destination drive is full", "no permission"…
- **Retry failed** tries those files again with the same settings once you've fixed the cause.
- From the report: tick the files in the results list, show them in their folder, copy the list
  or export it to CSV. FileHunter offers to open the report automatically when something wasn't copied.

## 1.1.1 — 2026-10-25

- Fixed **Skip files that are already there**: it now compares file *contents* with everything already
  in the destination folder (any name, any sub-folder). Before, it only checked a file with exactly the
  same name, so running the same copy again could create `photo (3).jpg`, `photo (4).jpg`…

## 1.1.0 — 2026-10-25

- New **Delete checked…** button: sends the ticked files to the Recycle Bin (Trash on macOS/Linux),
  or deletes them permanently after a second confirmation.
- Warns when files are on a pendrive, memory card or network drive (Windows deletes those
  permanently, there is no Recycle Bin there) and offers to leave program/system files alone.

## 1.0.0 — 2026-10-25

First release.

- Detects every drive: internal disks, partitions, pendrives, SD cards, USB disks, CD/DVD and
  network drives. New drives appear automatically when plugged in.
- Tick or untick drives, and add any extra folder to search.
- Ten ready-made file groups (Images, Videos, Audio, Documents, Archives, Programs, Disk images,
  Code, Fonts, Ebooks) plus any extensions you type.
- Filters: name contains (with * and ? wildcards), size range, date modified, hidden files,
  skip Windows/program/app-data folders.
- Scans every folder and sub-folder in the background, several drives at once, with pause and stop.
- Results list with tick boxes, sorting, live search, type filter, image preview,
  right-click menu, Open and Show in folder.
- Duplicate finder (compares real file contents) and "tick every extra copy".
- Copy or move (cut & paste) into one folder, by file type, by month, or keeping the original
  structure. Name-clash handling, skip identical files, optional verification, free-space check,
  pause/stop, and undo of the last copy or move.
- Export results to CSV, copy paths, activity log, light/dark theme.
