"""EduBotics Servo-Station: the vendor's bench tool that calibrates every V1
servo once — Referenz lesen (P0), Wareneingang, Kalibrieren, Prüfen, Lehre
prüfen. Start with ``Servo-Station.cmd`` (Windows) or ``python station_app.py``.

The modules import each other by plain name (``station_app.py`` puts this
directory and ``robotis_ai_setup/docker/open_manipulator`` on ``sys.path``);
only ``station_app.py`` imports tkinter, and nothing imports pyserial at module
level, so the logic is tested deps-free under ``robotis_ai_setup/tests``.
"""
