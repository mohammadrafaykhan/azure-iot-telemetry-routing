from importlib.util import find_spec

# azure-functions 1.x supports Python up to 3.12, the version the function app runs on; on newer
# interpreters the binding tests are skipped while the pipeline logic is still tested.
collect_ignore = [] if find_spec("azure.functions") else ["test_function_app.py"]
