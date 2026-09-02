# A Truly Usable HDR Calibration Tool for Windows 11
[中文](https://github.com/forbxy/rwhc/blob/master/README_zh.md)|ENGLISH

![screenshot](https://github.com/forbxy/rwhc/blob/master/resources/ui.png)

## Usage

1. **Get the Project Code**  
   Download or clone this repository and go to the project root directory.

2. **Install Python**  
   Install Python on Windows (choose one of the following):
   - Microsoft Store
   - Official website: <https://www.python.org/downloads/windows/>

3. **Install Dependencies**  
   In the project root directory, run:

   ```bash
   pip install -r requirements.txt
   ```

4. **Run the Program**

   ```bash
   python app.py
   ```
   Click the “Calibrate” button to start calibration.

## Color Measurement Devices

Currently the device driver is based on ArgyllCMS, which supports common devices such as the full X-Rite lineup and Datacolor devices.  
For the full list, please refer to the official documentation.

## Additional Usage Notes

1. **Using X-Rite Colorimeters with Logitech Mice**  
   Logitech mouse drivers may aggressively scan all devices and end up occupying the X-Rite colorimeter.  
   First, change the mouse driver in Device Manager back to the default Windows driver, then go to “Services” and stop the `Logitech LampArray Service`.

2. **Using Datacolor Spyder Colorimeters**  
   Click the “Install driver” button to open the Argyll website, download the driver, then run the installer in the `usb` folder.  
   After that, in Device Manager find the Spyder device (under “Universal Serial Bus controllers”):
   - Right-click the device and choose “Update driver”
   - Choose “Browse my computer for drivers”
   - Choose “Let me pick from a list of available drivers on my computer”
   - Select the Argyll driver from the list

3. **SDR Content Brightness (paper white)**  
   The calibration anchors the SDR white patch and the test-set brightness to the **actual SDR white level of the selected display** — the same "SDR content brightness" slider you see in Windows settings — rather than a hard-coded value.  
   Before starting the app, set that slider to the brightness you actually use for SDR content.
   - How it is read: the app queries `DISPLAYCONFIG_SDR_WHITE_LEVEL` (raw 0–10000) and converts with `nits = raw / 1000 × 80`.  
     In slider terms (0–100): `nits = slider × 8` (slider 20 → 160 nits).
   - The white test patch code is recomputed at each calibration: `code = round(pq_oetf(paper_white) × 1023)` (160 nits → 569/1023).
   - If the system value cannot be read it falls back to 200 nits.
   - Note: the value is read **once at application start** — move the slider first, then launch the app.

4. **Number of Grayscale Samples**  
   10‑bit HDR has 1024 grayscale levels (R=G=B, range 0–1023).  
   The program will sample a specified number of grayscale points evenly from these 1024 levels and interpolate the unsampled levels.  
   More samples generally (but not always) mean more accurate PQ curve calibration, at the cost of longer measurement time.

5. **Color Sample Set**  
   The program generates a test set within the selected gamut, and fits a matrix from the relationship between the expected XYZ values and the measured XYZ values. Four tiers are available:
   - **sRGB(12)**: the original 12-color sRGB card
   - **sRGB(12)+DisplayP3(7)**: 12 sRGB colors + 7 Display-P3 colors (for wide-gamut displays where colors look dull)
   - **sRGB(24)**: the industry-standard 24-color card (X-Rite ColorChecker Classic)
   - **sRGB(24)+DisplayP3(7)**: the 24-color card + 7 Display-P3 colors
   More samples generally give a more robust matrix fit, at the cost of longer measurement time.

6. **Historical Gray Data (skip re-measuring the PQ curve)**  
   When making several calibration profiles with different color temperatures on the same display, the PQ grayscale curve does not need to be re-measured every time — the display's native response does not depend on the target white point, only the white-point adaptation changes.  
   Select a past complete grayscale measurement from the "Historical gray data" dropdown (each entry is labeled with its measurement end time), and the PQ curve step will reuse that data instead of measuring.  
   Click "Refresh" to reload the list (e.g. after a new calibration in this session); the list is also refreshed automatically when a calibration finishes.  
   Note: the historical data must come from the same display, and interrupted/incomplete measurements are not shown.

7. **Bright Mode**  
   Applies an overall boost to the generated LUT (1D LUT * 1.1).  
   This is only suitable for watching movies in strong ambient light.

8. **Preview Calibration Result**  
   After calibration, the matrix and LUT are stored in memory.  
   When “Preview calibration result” is checked, a temporary ICC profile will be generated and applied to the selected display.  
   When unchecked, the temporary profile is automatically removed.  
   
   If calibration has not been run yet, an ideal HDR ICC profile is loaded instead  
   (BT.2020 gamut, 10000 nits, identity matrix and unmodified LUT).

9. **Calibrate**  
   Generates the matrix and LUT.

10. **Measure Color Accuracy**  
    Measures the color accuracy of the display.  
    If “Preview calibration result” is checked, the current matrix and LUT are temporarily applied to the display before measurement.  
    The accuracy of this feature has not been deeply validated.

11. **Save**  
    Saves the matrix and LUT as an ICC profile.

## Integrated External Tools

- **Pattern / Color Generator**  
  dogegen  
  <https://github.com/ledoge/dogegen>

- **Colorimeter Driver / Measurement Tool**  
  ArgyllCMS `spotread`  
  <https://www.argyllcms.com/>  
  Since DisplayCAL also uses the same driver, you can also refer to DisplayCAL’s documentation.

## Notes on Colorimeter Calibration

Colorimeters require display‑type‑specific spectral correction files.  
For details, see:

- ArgyllCMS documentation: <https://www.argyllcms.com/doc/oeminst.html>  
- DisplayCAL related tutorials and documentation

## About This Project

The author does not work professionally in color science, so the calibration logic may not be optimal.  
If you find issues or have better ideas, contributions and feedback are very welcome.

See [CHANGELOG](CHANGELOG.md) for the version history (中文版见 [CHANGELOG_zh](CHANGELOG_zh.md)).

## License

This project is licensed under the GNU Affero General Public License v3.0 (AGPL‑3.0).  
See the `LICENSE` file in the project root for details.