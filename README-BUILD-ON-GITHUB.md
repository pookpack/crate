# Building the Mac version of Crate (no Mac required)

You don't own a Mac and neither does the machine that made the Windows version —
so we build the Mac app on one of GitHub's free Apple-Silicon Mac servers. You
click through a short setup once; the result is a real `Crate.app` your editor
just double-clicks. The build even tests itself before you ship it.

Total time: ~5 minutes of clicking + ~15 minutes of waiting for the build.

---

## Part A — you (on Windows): get the app built

1. **Make a free GitHub account** at <https://github.com> (skip if you have one).

2. **Create a new repository:** click the **+** (top-right) → **New repository**.
   - Name it `crate` (anything is fine).
   - Set it to **Public** — this makes the Mac build minutes free. (Private works
     too but uses your limited free Mac-build quota.)
   - Click **Create repository**.

3. **Upload the build files:** on the new repo page click **uploading an existing
   file** (or **Add file → Upload files**). Unzip **`Crate-Mac-Build-Kit.zip`**,
   then drag **everything inside it** — including the `.github`, `templates`, and
   `build_tools` folders — into the upload box. Click **Commit changes**.

4. The build starts on its own. Click the **Actions** tab. You'll see a run called
   **“Build Crate for macOS (Apple Silicon)”** with a spinning yellow dot.
   - If GitHub asks you to enable Actions first, click the green **enable** button,
     then open the workflow and click **Run workflow**.

5. **Wait for the green check** (~15–20 min). If it goes red, open the run, expand
   the failed step, and send me the red error text — I'll fix it.

6. **Download the app:** open the finished (green) run, scroll to **Artifacts** at
   the bottom, and download **`Crate-macOS-arm64`**. It's a `.zip`.

7. **Send that `.zip` to your editor.**

---

## Part B — your editor (on the Mac): run it

1. **Unzip** the file → `Crate.app`. Drag it to **Applications** (optional).

2. **First open (one time):** because the app isn't signed by Apple, don't
   double-click it the first time. Instead **right-click (or Control-click)
   `Crate.app` → Open → Open**.
   - If macOS still says *“Crate is damaged / can't be opened”*, open the
     **Terminal** app and run this one line (adjust the path if needed):
     ```
     xattr -dr com.apple.quarantine /Applications/Crate.app
     ```
     then open it normally. This is only needed once.

3. Crate opens in the browser and shows a **Dock icon**. Set a destination folder,
   add boxes, paste links — same as the Windows version.

4. **To quit:** click **Quit** (top-right of the Crate page), or press **⌘Q**, or
   right-click the Dock icon → **Quit**.

**One note on article screenshots:** the first launch quietly downloads a browser
engine (~150 MB, needs internet) in the background. Images and videos work
immediately; if you try an *article* link in the first minute or two after the very
first launch, give it a moment and retry once the download finishes. After that
it's always ready, online or off.
