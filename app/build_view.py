"""The view is vanilla source: validation is the build, web/ is its dist."""
from pathlib import Path
root = Path(__file__).resolve().parent.parent
for name in ('index.html', 'boot.js', 'wizard-ui.css', 'styles.css', 'theme-client.js', 'app.js'):
    assert (root / 'web' / name).is_file(), name
print('View built: web/ (vanilla source, no dependencies)')
