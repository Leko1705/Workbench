from workbench.server import WorkbenchServer

if __name__ == "__main__":
    WorkbenchServer().run(host="127.0.0.1", port=8000, admin_password="1234")  # password from ADMIN_PASSWORD env
