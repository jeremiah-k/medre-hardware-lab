import os
import tempfile

os.environ.setdefault("MEDRE_LAB_RUN_DIR", tempfile.mkdtemp(prefix="medre-lab-tests-"))
