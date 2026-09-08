"""MongoDB Migration Tool: Local to Production.

Transfers all collections, documents, GridFS files/chunks (avatars, logos),
and indexes from a local MongoDB instance to a production MongoDB instance
(e.g., MongoDB Atlas, cloud VPS, or remote replica set).

Usage:
    # 1. Migrate all data from local to production:
    python migrate_mongo.py --target-uri "mongodb+srv://user:password@cluster.mongodb.net/?retryWrites=true&w=majority"

    # 2. Verify / compare counts without writing:
    python migrate_mongo.py --target-uri "mongodb+srv://user:password@cluster.mongodb.net" --verify-only

    # 3. Custom source/target DB names:
    python migrate_mongo.py --source-uri "mongodb://localhost:27017" --source-db brand_intelligence --target-uri "mongodb+srv://..." --target-db brand_intelligence
"""

from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from pymongo import MongoClient
from pymongo.errors import BulkWriteError, ConnectionFailure, ServerSelectionTimeoutError


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate Brand Intelligence MongoDB from local to production"
    )
    parser.add_argument(
        "--source-uri",
        default="mongodb://localhost:27017",
        help="Source MongoDB connection URI (default: mongodb://localhost:27017)",
    )
    parser.add_argument(
        "--source-db",
        default="brand_intelligence",
        help="Source database name (default: brand_intelligence)",
    )
    parser.add_argument(
        "--target-uri",
        required=True,
        help="Target (production) MongoDB URI (e.g., mongodb+srv://user:pass@cluster.mongodb.net)",
    )
    parser.add_argument(
        "--target-db",
        default="",
        help="Target database name (default: same as source-db)",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=500,
        help="Batch size for copying documents (default: 500)",
    )
    parser.add_argument(
        "--drop-existing",
        action="store_true",
        help="Drop target collections if they already exist before copying",
    )
    parser.add_argument(
        "--verify-only",
        action="store_true",
        help="Only compare document counts between source and target, do not write",
    )
    return parser.parse_args()


def get_client(uri: str, label: str) -> MongoClient:
    print(f"Connecting to {label} MongoDB: {uri.split('@')[-1] if '@' in uri else uri}...")
    try:
        c = MongoClient(uri, serverSelectionTimeoutMS=8000)
        c.admin.command("ping")
        print(f"  [OK] Connected to {label} successfully.")
        return c
    except (ConnectionFailure, ServerSelectionTimeoutError) as e:
        print(f"\n[ERROR] Failed to connect to {label} MongoDB:")
        print(f"  {type(e).__name__}: {e}")
        print("\nCommon fixes for Production MongoDB (e.g., MongoDB Atlas):")
        print("  1. Verify network access / IP allowlist (add your current public IP in Atlas).")
        print("  2. Check username and password (escape special characters like @, #, : with URL encoding).")
        print("  3. Ensure 'dnspython' is installed if using 'mongodb+srv://'.")
        sys.exit(1)


def main() -> None:
    args = parse_args()
    target_db_name = args.target_db if args.target_db else args.source_db

    print("=" * 65)
    print(" BRAND INTELLIGENCE - MONGODB MIGRATION TOOL")
    print("=" * 65)
    print(f"Source: {args.source_uri} -> DB: {args.source_db}")
    print(f"Target: {args.target_uri.split('@')[-1] if '@' in args.target_uri else args.target_uri} -> DB: {target_db_name}")
    print("=" * 65)

    source_client = get_client(args.source_uri, "SOURCE (Local)")
    target_client = get_client(args.target_uri, "TARGET (Production)")

    s_db = source_client[args.source_db]
    t_db = target_client[target_db_name]

    source_collections = s_db.list_collection_names()
    # Filter out system collections
    collections_to_migrate = [c for c in source_collections if not c.startswith("system.")]

    if not collections_to_migrate:
        print(f"\n[WARNING] No collections found in source database '{args.source_db}'. Nothing to migrate.")
        return

    print(f"\nFound {len(collections_to_migrate)} collections in source:")
    for col_name in sorted(collections_to_migrate):
        cnt = s_db[col_name].count_documents({})
        print(f"  - {col_name:25} ({cnt:,} docs)")

    if args.verify_only:
        print("\n--- VERIFY ONLY MODE (no writes performed) ---")
        print(f"{'Collection':30} {'Source Count':15} {'Target Count':15} {'Status'}")
        print("-" * 70)
        for col_name in sorted(collections_to_migrate):
            s_cnt = s_db[col_name].count_documents({})
            t_cnt = t_db[col_name].count_documents({})
            status = "[MATCH]" if s_cnt == t_cnt else "[MISMATCH]"
            print(f"{col_name:30} {s_cnt:15,} {t_cnt:15,} {status}")
        return

    print("\nStarting migration...")
    start_total = time.time()
    total_docs_migrated = 0

    for col_name in collections_to_migrate:
        s_coll = s_db[col_name]
        t_coll = t_db[col_name]

        total_source_docs = s_coll.count_documents({})
        if total_source_docs == 0:
            print(f"\n[SKIP] {col_name}: 0 documents.")
            continue

        if args.drop_existing:
            print(f"\nDropping target collection '{col_name}' as requested...")
            t_coll.drop()

        print(f"\nProcessing '{col_name}' ({total_source_docs:,} documents)...")
        cursor = s_coll.find({}, no_cursor_timeout=True)
        batch: list[dict[str, Any]] = []
        col_migrated = 0

        try:
            for doc in cursor:
                batch.append(doc)
                if len(batch) >= args.batch_size:
                    try:
                        t_coll.insert_many(batch, ordered=False)
                    except BulkWriteError as bwe:
                        # Ignore duplicate key errors (11000) if re-running
                        write_errors = bwe.details.get("writeErrors", [])
                        real_errors = [e for e in write_errors if e.get("code") != 11000]
                        if real_errors:
                            print(f"  Warning: {len(real_errors)} errors in batch: {real_errors[:2]}")
                    col_migrated += len(batch)
                    batch = []
                    sys.stdout.write(f"\r  Migrated {col_migrated:,} / {total_source_docs:,} docs...")
                    sys.stdout.flush()

            if batch:
                try:
                    t_coll.insert_many(batch, ordered=False)
                except BulkWriteError as bwe:
                    write_errors = bwe.details.get("writeErrors", [])
                    real_errors = [e for e in write_errors if e.get("code") != 11000]
                    if real_errors:
                        print(f"  Warning: {len(real_errors)} errors in final batch: {real_errors[:2]}")
                col_migrated += len(batch)
                sys.stdout.write(f"\r  Migrated {col_migrated:,} / {total_source_docs:,} docs...")
                sys.stdout.flush()

            print(f"\n  [OK] Done '{col_name}'.")
            total_docs_migrated += col_migrated

            # Copy indexes from source to target
            indexes = s_coll.index_information()
            for idx_name, idx_info in indexes.items():
                if idx_name == "_id_":
                    continue
                keys = idx_info["key"]
                options: dict[str, Any] = {"name": idx_name}
                if "unique" in idx_info:
                    options["unique"] = idx_info["unique"]
                if "partialFilterExpression" in idx_info:
                    options["partialFilterExpression"] = idx_info["partialFilterExpression"]
                try:
                    t_coll.create_index(keys, **options)
                except Exception as e:
                    print(f"    Index notice on {idx_name}: {e}")

        finally:
            cursor.close()

    elapsed = time.time() - start_total
    print("\n" + "=" * 65)
    print(f" MIGRATION COMPLETE in {elapsed:.1f}s — {total_docs_migrated:,} docs transferred")
    print("=" * 65)

    print("\nVerification Summary:")
    print(f"{'Collection':30} {'Source Count':15} {'Target Count':15} {'Status'}")
    print("-" * 70)
    for col_name in sorted(collections_to_migrate):
        s_cnt = s_db[col_name].count_documents({})
        t_cnt = t_db[col_name].count_documents({})
        status = "[MATCH]" if s_cnt == t_cnt else "[MISMATCH]"
        print(f"{col_name:30} {s_cnt:15,} {t_cnt:15,} {status}")

    print("\nNext Steps:")
    print("1. Update your .env (or server environment variables):")
    print(f"   MONGO_URI=\"{args.target_uri}\"")
    print(f"   MONGO_DB_NAME=\"{target_db_name}\"")
    print("2. Restart the backend service.")
    print("=" * 65)


if __name__ == "__main__":
    main()
