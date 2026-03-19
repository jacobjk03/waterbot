"""
Add a single file to the pgvector RAG store. For batch ingestion use Add_files_to_db.py.
Requires DB_HOST, DB_USER, DB_PASSWORD, DB_NAME and OPENAI_API_KEY for embeddings.
"""
import os
import re
import sys
import uuid

_script_dir = os.path.dirname(os.path.abspath(__file__))
_application_dir = os.path.dirname(_script_dir)
if _application_dir not in sys.path:
    sys.path.insert(0, _application_dir)

from dotenv import load_dotenv
from langchain_community.document_loaders import PyPDFLoader, TextLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings

load_dotenv(os.path.join(_application_dir, ".env"))


def main():
    file_path = os.path.join(_application_dir, "newData", "Where does our water come from_ _ Arizona Environment.pdf")
    if len(sys.argv) > 1:
        file_path = sys.argv[1]
    if not os.path.isfile(file_path):
        print(f"File not found: {file_path}")
        sys.exit(1)

    db_host = os.getenv("DB_HOST")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    db_name = os.getenv("DB_NAME")
    if not all([db_host, db_user, db_password, db_name]):
        print("Set DB_HOST, DB_USER, DB_PASSWORD, DB_NAME")
        sys.exit(1)

    from managers.pgvector_store import PgVectorStore
    embeddings = OpenAIEmbeddings()
    store = PgVectorStore(
        db_params={"dbname": db_name, "user": db_user, "password": db_password, "host": db_host, "port": "5432"},
        embedding_function=embeddings,
    )

    text_splitter = RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=150)
    file_name = os.path.basename(file_path)
    if file_path.lower().endswith(".pdf"):
        loader = PyPDFLoader(file_path)
    elif file_path.lower().endswith(".txt"):
        loader = TextLoader(file_path, encoding="utf-8")
    else:
        print("Only PDF and TXT are supported.")
        sys.exit(1)

    data = loader.load()
    splits = []
    for doc in data:
        if not doc.page_content or not doc.page_content.strip():
            continue
        doc.metadata["id"] = str(uuid.uuid4())
        doc.metadata["source"] = file_path
        doc.metadata["name"] = file_name
        for chunk in text_splitter.split_documents([doc]):
            chunk.metadata = doc.metadata.copy()
            splits.append(chunk)

    if not splits:
        print("No content extracted.")
        sys.exit(1)
    store.add_documents(splits, locale="en")
    print(f"Successfully added {len(splits)} chunks to pgvector.")


if __name__ == "__main__":
    main()
