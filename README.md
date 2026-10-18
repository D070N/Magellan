# Suport the project

Everything in here is completely free. There are no ads, monthly subscriptions, or paywalls! So if you feel generous and you like the app, you can [support via PayPal](https://www.paypal.com/donate/?business=LL62UHZCBYWS2&no_recurring=0&item_name=Thank+you+%28%24o%24%29&currency_code=BRL)

# Magellan

Magellan is a Windows desktop file and media browser built with Python and Qt. The application source and Windows build files are in [`MediaExplorer/`](MediaExplorer/); the root icon and `Alternative Icons/` assets are required by the app and its executable build.

## Features

- Browse folders and media while looking at thumbnails with adjustable size, tabs that persist even after closing the app, natural sorting, unobtrusive and configurable tooltips, and panels:

<img width="1919" height="1079" alt="image" src="https://github.com/user-attachments/assets/321cb287-55af-4af9-a0a2-52f9fd4a5853" />

- Image search mode:

<img width="421" height="295" alt="image" src="https://github.com/user-attachments/assets/795accee-b1c3-4096-b9b7-213b94993340" /> <img width="679" height="698" alt="image" src="https://github.com/user-attachments/assets/25226686-339f-444b-8d5f-a8b954b7fadb" />

If you are like me and always need to find more about a work of art or simply find the artist behind it, with the press of a button, you can send an image to Google Lens or SauceNAO. The result of your search will appear directly in your browser, no hassle! You can input your SauceNAO API key from your free account to increase your search usage. There is also an option for the Google image search to attach a text query to the image, increasing the possibilities for different types of image search.

- Group image visualization system. "Farview" mode creates a continuous list of images and files for quick visualization. Now you won't have to suffer with hundreds of single-image folders like I once did, because Farview takes all of those pictures and files and lists them as if they were from the same folder:

https://github.com/user-attachments/assets/d7a6019e-b3c0-4ef0-8169-747d85d465b0

As you can see you can easely select your desired folders to create a Farview, or you can simply press the button directely to create a Farview of the entire local page. 

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

- With the organization mode you can also set you own personal folder order and use it on Farview:

https://github.com/user-attachments/assets/0b690a1b-efca-49df-a0c4-f26084e79414

- Generate thumbnails asynchronously and cache them for smoother scrolling:

<img width="279" height="263" alt="image" src="https://github.com/user-attachments/assets/a1151486-5f18-4126-ac31-54793e689113" />

- Search the current folder, with optional recursive search under **Performance**.

- Browse folder trees with background loading and paged child lists:

<img width="297" height="197" alt="image" src="https://github.com/user-attachments/assets/eda0e224-4124-420f-9035-614e62e82dc1" />
  
- View images, animated images, PDFs, and supported video formats; very large images are decoded at a bounded resolution while retaining their original dimensions in the viewer details. This means that BIG pictures are supported! I've tested up to 16K pictures.
  
- Save preferences and tabs locally. Application logs are bounded by the configured limit in **Performance**.

<img width="255" height="167" alt="image" src="https://github.com/user-attachments/assets/a80c6cdb-67f9-49d0-841f-37925f0ec921" />

- Useful tools:

<img width="191" height="236" alt="image" src="https://github.com/user-attachments/assets/f32f30ab-42c9-4a76-9375-8491002976cd" />

Visualization options:

<img width="213" height="297" alt="image" src="https://github.com/user-attachments/assets/13964156-fe03-46fa-94a6-1aeaa6566379" />

## Controls:

I've made the controls thinking about optimal navigation efficiency. So, you might need to get used to it, but I promise it will be worth it. You can find all of them in the HELP menu along with their complete functionalities.

- Navigation:

Single left-click: Enters a folder and opens files.

Right-click: Goes one page "up," like clicking the "Back" arrow in your browser or Windows Explorer.

Middle Mouse-click: Opens the selected folder into a new tab and closes hovered tabs. 

Shift+Left-click: Selection mode. Drag to define a selection area, and click to toggle the selection. 

Alt+Left Click: Opens the containing folder of the targeted file.

Ctrl+Left-click: Hold to enable organization mode.

Ctrl+A: Selects and de-selects all files in the current page.

Shift+Del: Sends selected files to trash.

Ctrl+Z: Undoes the DE-selection or restores recently deleted files.

Arows: Moves the selection highlight.

Backspace: Goes back a folder.

Enter: Enters a folder.

1, 2, 3, 4, 5, 6: Toggles between sorting modes.

- Image Viewer controls;

Arrows: Swipes controls. The "up" arrow closes the viewer.

W, A, S, D: Repositions image when zooming.

Q and W: Previous and Next image.

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

Run `MediaExplorer\build_windows.bat` from a Windows checkout. It installs the Python dependencies and PyInstaller, then writes `MediaExplorer\dist\Magellan.exe`.

## Local settings and privacy

Settings and logs are stored in the per-user application-data directory. The optional SauceNAO API key is stored in the local settings file and is not included in this repository.

## License

Magellan is distributed under the MIT License. See [LICENSE](LICENSE).
