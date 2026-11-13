import os
import tempfile

# llm_modelfit lit sa liste de plateformes dans une base SQLite : base jetable pour les tests
os.environ["LLM_MODELFIT_DB"] = os.path.join(tempfile.mkdtemp(), "test.db")
os.environ["EDGEFIT_DB"] = os.path.join(tempfile.mkdtemp(), "edgefit-test.db")
