# WuWa Mod Importer

Import Wuthering Waves WWMI mods into Blender from their INI files, mesh buffers, and textures.

Created by **jinshi_lover**. Version **1.0**. Tested with **Blender 5.0.1**.

## Features

- Imports meshes, UVs, vertex colors, skin weights, and available shape keys.
- Finds mod INI files automatically, including inside nested folders.
- Assigns textures using mod references and image analysis.
- Imports outfit and texture variants without duplicating shared geometry.
- Adds model toggle controls under **N sidebar > WWMI**.
- Supports packed and grayscale tacet mark masks.
- Adds supported RabbitFX effects with a separate collection toggle.
- Includes optional WuWa-inspired shading for EEVEE and Cycles.
- Runs as a standalone script with no add-on installation required.

## Usage

1. Download `wuwa_mod_importer_1.0.py`.
2. Open it in Blender's **Text Editor**.
3. Set `MOD_FOLDER` to your extracted mod folder or its INI file:

   ```python
   MOD_FOLDER = Path(r"C:\path\to\mod")
   ```

4. Click **Run Script**.
5. Select an imported part and open **N sidebar > WWMI** to change available toggles.

Use the **RabbitFX Effects** and **WuWa Appearance** collection checkboxes in the Outliner to switch their effects on or off. Use Rendered view, or enable **Scene Lights** and **Scene World** in Material Preview.

## Notes

- WuWa Appearance approximates the game's style. It does not reproduce the full game renderer. It was provided as a cosmetic feature.
- Texture roles may be inferred when the mod does not provide explicit bindings.
- Skin weights use numeric vertex groups. The script does not create a skeleton.
- After reopening a saved `.blend`, run its embedded control scripts from the Text Editor if the toggles are inactive.
