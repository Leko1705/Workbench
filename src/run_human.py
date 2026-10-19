from workbench.worker import HumanWorker

if __name__ == "__main__":
    person = HumanWorker(
        name="Human",
        description="A person: answers questions, approves work and takes part on the board.",
    )
    person.run(host="127.0.0.1", port=9998, admin_password="1234")  # password from ADMIN_PASSWORD env
