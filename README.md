# Magellan

Magellan is a Windows desktop file and media browser built with Python and Qt. The application source and Windows build files are in [`MediaExplorer/`](MediaExplorer/); the root icon and `Alternative Icons/` assets are required by the app and its executable build.

## Features

- Browse folders and media while looking at thumbnails with adjustable size, tabs that persist even after closing the app, natural sorting, unobtrusive and configurable tooltips, and panels:

<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/321cb287-55af-4af9-a0a2-52f9fd4a5853" />

- Image search mode:

<img width="1919" height="333" alt="image" src="https://github.com/user-attachments/assets/393443c1-2faf-4c17-aee7-c199c75bf515" />

If you are like me and always need to find more about a work of art or simply find the artist behind it, with the press of a button, you can send an image to Google Lens or SauceNAO. And the result of your search will appear in your browser, no hassle! You can also input your SauceNAO API key from your free account to increase your search usage.

- Group image visualization system. "Farview" mode creates a continuous list of images and files for quick visualization. Now you won't have to suffer with hundreds of single-image folders like I once did, because Farview takes all of those pictures and files and lists them as if they were from the same folder:

Before, the folders needed to be clicked, one by one, for you to reach a single or very few images, creating a touristic limbo of clicking:
<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/b9ddd349-b6a1-4a89-bdfa-7a50961c635e" />

After, each picture from each folder is listed and available for you with one click:
<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/683271b4-e93e-446e-85d4-4a4de858b95d" />

Farview also enables you to create a list from a specific selection, and yes, we have a selection feature:

<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/7aa0b6ab-0846-43b5-8624-99b933ed2e8d" />

- Now with the folders perfectly ordered, you can make use of our local image viewer to swipe the list you just created:

<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/d48f111a-0e87-4ea1-8fbe-777fa3156347" />

The main idea behind the image viewer is to completely remove any obstructions from your view and let you enjoy your pictures as they should be enjoyed. All of its buttons are hidden when they are not in use. When you need to know more about a picture or skip to the next, you can hover around and expose its info tooltip and buttons:

<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/d4f2791c-0079-4d16-aa9b-4c3340d50eab" />

The viewer also has a slideshow mode, double-page visualization, and flipping options. The Slidshow timer is configurable from the "VIEW" menu.

- Selection mode:

 <img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/dbb15b33-ec7d-46ed-84b7-0c6d9f6f6e56" />

As you can see, it's very similar to Windows selection mode, BUT, it persists even after changing folders, allowing you to not worry so much about losing a selection, speaking of which:

<img width="265" height="227" alt="image" src="https://github.com/user-attachments/assets/77dc98f0-18d9-4c9f-af46-7cefe5b8af0e" />

If you lost a selection, you can simply "Ctrl+Z" or click the hidden side button above to reselect your last selection. No worries!

- Magellan also offers you a favorite menu, which can be easily flooded by all of your 135 favorite pictures at once, using the selection feature:

<img width="1919" height="1077" alt="image" src="https://github.com/user-attachments/assets/440f8662-7432-4e6a-82a4-3c6b3241c071" />

The favorites menu, accessible by pressing the Star button on the main panel, has side buttons to help you navigate it:

<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/ab8f2924-7bf5-4f17-8167-706267176da1" />

Here you can create favorite groups, use the organization mode to drag files and folders to specific groups, and access your usual selection buttons.

- Generate thumbnails asynchronously and cache them for smoother scrolling:

<img width="292" height="188" alt="image" src="https://github.com/user-attachments/assets/a7266283-6f5f-4039-b5d6-4c35c873cb60" />

- Search the current folder, with optional recursive search under **Performance**.

- Browse folder trees with background loading and paged child lists:

<img width="297" height="197" alt="image" src="https://github.com/user-attachments/assets/eda0e224-4124-420f-9035-614e62e82dc1" />
  
- View images, animated images, PDFs, and supported video formats; very large images are decoded at a bounded resolution while retaining their original dimensions in the viewer details. This means that BIG pictures are supported! I've tested up to 16K pictures.
  
- Save preferences and tabs locally. Application logs are bounded by the configured limit in **Performance**.

<img width="255" height="167" alt="image" src="https://github.com/user-attachments/assets/a80c6cdb-67f9-49d0-841f-37925f0ec921" />

- Useful tools:

<img width="193" height="181" alt="image" src="https://github.com/user-attachments/assets/c14a746e-bb94-4896-b8a0-548071968586" />

Visualization options:

<img width="179" height="211" alt="image" src="https://github.com/user-attachments/assets/7c46bc0c-f160-481d-a061-54759029df76" />

## Controls:

I've made the controls thinking about optimal navigation efficiency. So, you might need to get used to it, but I promise it will be worth it.

- Navigation:

Single left-click: Enters a folder and opens files.

Right-click: Goes one page "up," like clicking the "Back" arrow in your browser or Windows Explorer.

Middle Mouse-click: Opens the selected folder into a new tab and closes hovered tabs. 

Shift+Left-click: Selection mode. Drag to define a selection area, and click to toggle the selection. 

Alt+Left Click: Clears the current selection

Ctrl+Left-click: Only used to toggle the selection.

Shift+Del: Sends selected files to trash.

Ctrl+Z: Undoes the DE-selection or restores recently deleted files.

Arows: Moves the selection highlight.

Backspace: Goes back a folder.

Enter: Enters a folder.

1, 2, 3, 4, 5, 6: Toggles between sorting modes.

- Image Viewer controls;

Arrows: Swipes controls. The "up" arrow closes the viewer.

R: Rotate image

F: Flip image

H: Toggle slideshow.

Del: Sends image to trash.


## Run from source

Install Python 3.10 or newer, then run these commands from `MediaExplorer`:

```powershell
py -m pip install -r requirements.txt
py main.py
```

The application expects `MainIcon.ico` and `Alternative Icons/` in the repository root.

## Build on Windows

Run `MediaExplorer\build_windows.bat` from a Windows checkout. It installs the Python dependencies and PyInstaller, then writes `MediaExplorer\dist\Magellan.exe`. Build output and thumbnail caches are intentionally excluded from Git; distribute executables through GitHub Releases rather than committing them to the source repository.

## Local settings and privacy

Settings and logs are stored in the per-user application-data directory. The optional SauceNAO API key is stored in the local settings file and is not included in this repository.

## License

Magellan is distributed under the MIT License. See [LICENSE](LICENSE).
