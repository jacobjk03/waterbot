import os
import re
import sys
import uuid

# Ensure application root is on path when running as script
_script_dir = os.path.dirname(os.path.abspath(__file__))
_application_dir = os.path.dirname(_script_dir)
if _application_dir not in sys.path:
    sys.path.insert(0, _application_dir)

from dotenv import load_dotenv
from langchain_community.document_loaders import TextLoader, PyPDFLoader
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_openai import OpenAIEmbeddings

# Load .env from project root (waterbot/.env) where OPENAI_API_KEY (embeddings), DATABASE_URL, etc. live
_project_root = os.path.dirname(_application_dir)
load_dotenv(os.path.join(_project_root, ".env"))

LOCALE = "en"


def add_document_with_metadata(store, text_splitter, file_path, splits):
    file_name = os.path.basename(file_path)

    try:
        if not os.path.exists(file_path):
            print(f"⚠️  Skipping {file_path}: File does not exist", file=sys.stderr)
            return

        file_size = os.path.getsize(file_path)
        if file_size == 0:
            print(f"⚠️  Skipping {file_path}: File is empty (0 bytes)", file=sys.stderr)
            return

        if bool(re.match(r".*\.txt$", file_path, re.IGNORECASE)):
            loader = TextLoader(file_path, encoding='utf-8')
        elif bool(re.match(r".*\.pdf$", file_path, re.IGNORECASE)):
            loader = PyPDFLoader(file_path)
        else:
            return

        data = loader.load()

        if not data or len(data) == 0:
            print(f"⚠️  Skipping {file_path}: No content extracted", file=sys.stderr)
            return

        print(f"📄 Processing {file_name} ({len(data)} pages/chunks)")

        for doc in data:
            if not doc.page_content or len(doc.page_content.strip()) == 0:
                continue

            print(f"Adding : {file_path}")
            doc.metadata['id'] = str(uuid.uuid4())
            doc.metadata['source'] = file_path
            doc.metadata['name'] = file_name

            chunks = text_splitter.split_documents([doc])
            for chunk in chunks:
                chunk.metadata = doc.metadata.copy()
                splits.append(chunk)
    except Exception as e:
        print(f"⚠️  Skipping {file_path}: Error - {str(e)}", file=sys.stderr)
        return


def get_store(application_dir):
    embeddings = OpenAIEmbeddings()
    database_url = os.getenv("DATABASE_URL")
    db_host = os.getenv("DB_HOST")
    db_user = os.getenv("DB_USER")
    db_password = os.getenv("DB_PASSWORD")
    db_name = os.getenv("DB_NAME")
    db_port = os.getenv("DB_PORT", "5432")
    if database_url:
        from managers.pgvector_store import PgVectorStore
        print("✅ PgVector store initialized (locale=en) via DATABASE_URL")
        return PgVectorStore(db_url=database_url, embedding_function=embeddings)
    if not all([db_host, db_user, db_password, db_name]):
        print("❌ pgvector requires DATABASE_URL or DB_HOST, DB_USER, DB_PASSWORD, DB_NAME", file=sys.stderr)
        sys.exit(1)
    from managers.pgvector_store import PgVectorStore
    print("✅ PgVector store initialized (locale=en)")
    return PgVectorStore(
        db_params={"dbname": db_name, "user": db_user, "password": db_password, "host": db_host, "port": db_port},
        embedding_function=embeddings,
    )


def main():
    print("🚀 Starting RAG loader...")
    print(f"Working directory: {os.getcwd()}")
    print("Backend: pgvector")
    print(f"OPENAI_API_KEY (embeddings): {'SET' if os.getenv('OPENAI_API_KEY') else 'NOT SET'}")

    application_dir = _application_dir
    project_root = os.path.dirname(application_dir)

    possible_paths = [
        os.path.join(application_dir, 'newData'),
        os.path.join(project_root, 'application', 'newData'),
        os.path.join(os.getcwd(), 'newData'),
        os.path.join(os.getcwd(), 'application', 'newData'),
    ]

    directory_path = None
    for path in possible_paths:
        if os.path.exists(path):
            directory_path = path
            print(f"✅ Found newData directory at: {directory_path}")
            break

    if not directory_path:
        print("❌ Could not find newData directory.", file=sys.stderr)
        sys.exit(1)

    try:
        text_splitter = RecursiveCharacterTextSplitter(chunk_size=1500, chunk_overlap=150)
        store = get_store(application_dir)
    except Exception as e:
        print(f"❌ Failed to initialize store: {str(e)}", file=sys.stderr)
        sys.exit(1)

    pdf_count = len([f for f in os.listdir(directory_path) if f.lower().endswith('.pdf')])
    print(f"📄 Found {pdf_count} PDF files in {directory_path}")

    batch_size = 20
    batch = []
    batch_count = 0

    for root, dirs, files in os.walk(directory_path):
        for file in files:
            file_path = os.path.join(root, file)
            batch.append(file_path)
            if len(batch) >= batch_size:
                process_batch(batch, store, text_splitter)
                batch = []
                batch_count += 1
                print(f"Batch {batch_count} processed.")

    if batch:
        process_batch(batch, store, text_splitter)
        print(f"Final batch {batch_count + 1} processed.")

    print("✅ RAG loading complete!")


def process_batch(batch, store, text_splitter):
    splits = []
    successful_files = 0
    skipped_files = 0

    for file_path in batch:
        initial_count = len(splits)
        add_document_with_metadata(store, text_splitter, file_path, splits)
        if len(splits) > initial_count:
            successful_files += 1
        else:
            skipped_files += 1

    if splits:
        try:
            store.add_documents(splits, locale=LOCALE)
            print(f"✅ Successfully added {len(splits)} document chunks from {successful_files} files.")
            if skipped_files > 0:
                print(f"⚠️  Skipped {skipped_files} files in this batch (empty/corrupted)")
        except Exception as e:
            print(f"❌ Failed to add documents: {e}", file=sys.stderr)
    else:
        print(f"⚠️  No documents to add from this batch ({skipped_files} files skipped)")


if __name__ == "__main__":
    main()
