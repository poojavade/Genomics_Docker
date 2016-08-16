    #!/usr/bin/env python

import os
import sys
import sqlite3
from collections import defaultdict, namedtuple
import json
import subprocess

import numpy as np
from scipy.stats import mode
import pysam

from gemini.annotations import annotations_in_region, guess_contig_naming
from database import database_transaction

def add_requested_columns(args, update_cursor, col_names, col_types=None):
    """
    Attempt to add new, user-defined columns to the
    variants table.  Warn if the column already exists.
    """

    if args.anno_type in ["count", "boolean"]:

        col_name = col_names[0]
        col_type = "integer"

        try:
            alter_qry = "ALTER TABLE variants ADD COLUMN " \
                        + col_name \
                        + " " \
                        + col_type \
                        + " " \
                        + "DEFAULT NULL"
            update_cursor.execute(alter_qry)
        except sqlite3.OperationalError:
            sys.stderr.write("WARNING: Column \"("
                             + col_name
                             + ")\" already exists in variants table. Overwriting values.\n")
    elif args.anno_type == "extract":

        for col_name, col_type in zip(col_names, col_types):

            try:
                alter_qry = "ALTER TABLE variants ADD COLUMN " \
                            + col_name \
                            + " " \
                            + col_type \
                            + " " \
                            + "DEFAULT NULL"
                update_cursor.execute(alter_qry)
            except sqlite3.OperationalError:
                sys.stderr.write("WARNING: Column \"("
                                 + col_name
                                 + ")\" already exists in variants table. Overwriting values.\n")
    else:
        sys.exit("Unknown annotation type: %s\n" % args.anno_type)


def _annotate_variants(args, conn, get_val_fn, col_names=None, col_types=None, col_ops=None):
    """Generalized annotation of variants with a new column.

    get_val_fn takes a list of annotations in a region and returns
    the value for that region to update the database with.

    Separates selection and identification of values from update,
    to avoid concurrent database access errors from sqlite3, especially on
    NFS systems. The retained to_update list is small, but batching
    could help if memory issues emerge.
    """
    # For each, use Tabix to detect overlaps with the user-defined
    # annotation file.  Update the variant row with T/F if overlaps found.
    anno = pysam.Tabixfile(args.anno_file)
    naming = guess_contig_naming(anno)
    select_cursor = conn.cursor()
    update_cursor = conn.cursor()
    add_requested_columns(args, select_cursor, col_names, col_types)

    last_id = 0
    current_id = 0
    total = 0
    CHUNK_SIZE = 100000
    to_update = []

    select_cursor.execute('''SELECT chrom, start, end, variant_id FROM variants''')
    while True:
        for row in select_cursor.fetchmany(CHUNK_SIZE):

            # update_data starts out as a list of the values that should
            # be used to populate the new columns for the current row.
            update_data = get_val_fn(annotations_in_region(row, anno,
                                                           "tuple", naming))
            # were there any hits for this row?
            if len(update_data) > 0:
                # we add the primary key to update_data for the
                # where clause in the SQL UPDATE statement.
                update_data.append(str(row["variant_id"]))
                to_update.append(tuple(update_data))

            current_id = row["variant_id"]

        if current_id <= last_id:
            break
        else:
            update_cursor.execute("BEGIN TRANSACTION")
            _update_variants(to_update, col_names, update_cursor)
            update_cursor.execute("END TRANSACTION")

            total += len(to_update)
            print "updated", total, "variants"
            last_id = current_id
        to_update = []

def _update_variants(to_update, col_names, cursor):
        update_qry = "UPDATE variants SET "

        update_cols = ",".join(col_name + " = ?" for col_name in col_names)
        update_qry += update_cols
        update_qry += " WHERE variant_id = ?"
        cursor.executemany(update_qry, to_update)


def annotate_variants_bool(args, conn, col_names):
    """
    Populate a new, user-defined column in the variants
    table with a BOOLEAN indicating whether or not
    overlaps were detected between the variant and the
    annotation file.
    """
    def has_hit(hits):
        for hit in hits:
            return [1]
        return [0]

    return _annotate_variants(args, conn, has_hit, col_names)


def annotate_variants_count(args, conn, col_names):
    """
    Populate a new, user-defined column in the variants
    table with a INTEGER indicating the count of overlaps
    between the variant and the
    annotation file.
    """
    def get_hit_count(hits):
        return [len(list(hits))]

    return _annotate_variants(args, conn, get_hit_count, col_names)


def annotate_variants_extract(args, conn, col_names, col_types, col_ops, col_idxs):
    """
    Populate a new, user-defined column in the variants
    table based on the value(s) from a specific column.
    in the annotation file.
    """

    def _map_list_types(hit_list, col_type):
        try:
            if col_type == "int":
                return [int(h) for h in hit_list]
            elif col_type == "float":
                return [float(h) for h in hit_list]
        except ValueError:
            sys.exit('Non-numeric value found in annotation file: %s\n' % (','.join(hit_list)))

    def summarize_hits(hits):

        hits = list(hits)
        if len(hits) == 0:
            return []

        hit_list = defaultdict(list)
        for hit in hits:
            try:
                for idx, col_idx in enumerate(col_idxs):
                    hit_list[idx].append(hit[int(col_idx) - 1])
            except IndexError:
                sys.exit("EXITING: Column " + args.col_extracts + " exceeds "
                          "the number of columns in your "
                          "annotation file.\n")

        vals = []
        for idx, op in enumerate(col_ops):
            # more than one overlap, must summarize
            if op == "mean":
                val = np.average(_map_list_types(hit_list[idx], col_types[idx]))
            elif op == 'list':
                val = ",".join(hit_list[idx])
            elif op == 'uniq_list':
                val = ",".join(set(hit_list[idx]))
            elif op == 'median':
                val = np.median(_map_list_types(hit_list[idx], col_types[idx]))
            elif op == 'min':
                val = np.min(_map_list_types(hit_list[idx], col_types[idx]))
            elif op == 'max':
                val = np.max(_map_list_types(hit_list[idx], col_types[idx]))
            elif op == 'mode':
                val = mode(_map_list_types(hit_list[idx], col_types[idx]))[0][0]
            elif op == 'first':
                val = hit_list[idx][0]
            elif op == 'last':
                val = hit_list[idx][-1]
            else:
                sys.exit("EXITING: Operation (-o) \"" + op + "\" not recognized.\n")

            if col_types[idx] == "int":
                try:
                    vals.append(int(val))
                except ValueError:
                    if not val:
                        vals.append(None)
                    else:
                        sys.exit('Non-integer value found in annotation file: %s\n' % (val))
            elif col_types[idx] == "float":
                try:
                    vals.append(float(val))
                except ValueError:
                    if not val:
                        vals.append(None)
                    else:
                        sys.exit('Non-float value found in annotation file: %s\n' % (val))
            else:
                vals.append(val)

        return vals
    return _annotate_variants(args, conn, summarize_hits,
                              col_names, col_types, col_ops)

def annotate(parser, args):

    def _validate_args(args):
        if (args.col_operations or args.col_types or args.col_extracts):
            sys.exit('EXITING: You may only specify a column name (-c) when '
                     'using \"-a boolean\" or \"-a count\".\n')

        col_names = args.col_names.split(',')
        if len(col_names) > 1:
            sys.exit('EXITING: You may only specify a single column name (-c) '
                     'when using \"-a boolean\" or \"-a count\".\n')
        return col_names

    def _validate_extract_args(args):
        col_ops = args.col_operations.split(',')
        col_names = args.col_names.split(',')
        col_types = args.col_types.split(',')
        col_idxs = args.col_extracts.split(',')

        supported_types = ['text', 'float', 'integer']
        for col_type in col_types:
            if col_type not in supported_types:
                sys.exit('EXITING: Column type [%s] not supported.\n' %
                         (col_type))

        supported_ops = ['mean', 'median', 'mode', 'min', 'max', 'first',
                         'last', 'list', 'uniq_list']
        for col_op in col_ops:
            if col_op not in supported_ops:
                sys.exit('EXITING: Column operation [%s] not supported.\n' %
                         (col_op))

        if not (len(col_ops) == len(col_names) ==
                len(col_types) == len(col_idxs)):
            sys.exit('EXITING: The number of column names, numbers, types, and '
                     'operations must match: [%s], [%s], [%s], [%s]\n' %
                     (args.col_names, args.col_extracts, args.col_types, args.col_operations))

        return col_names, col_types, col_ops, col_idxs

    if (args.db is None):
        parser.print_help()
        exit(1)
    if not os.path.exists(args.db):
        sys.stderr.write("Error: cannot find database file.")
        exit(1)
    if not os.path.exists(args.anno_file):
        sys.stderr.write("Error: cannot find annotation file.")
        exit(1)

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row  # allow us to refer to columns by name
    conn.isolation_level = None

    if args.anno_type == "boolean":
        col_names = _validate_args(args)
        annotate_variants_bool(args, conn, col_names)
    elif args.anno_type == "count":
        col_names = _validate_args(args)
        annotate_variants_count(args, conn, col_names)
    elif args.anno_type == "extract":
        if args.col_extracts is None:
            sys.exit("You must specify which column to "
                     "extract from your annotation file.")
        else:
            col_names, col_types, col_ops, col_idxs = _validate_extract_args(args)
            annotate_variants_extract(args, conn, col_names, col_types, col_ops, col_idxs)
    else:
        sys.exit("Unknown column type requested. Exiting.")

    conn.close()

    # index on the newly created columns
    for col_name in col_names:
        with database_transaction(args.db) as c:
            c.execute('''create index %s on variants(%s)''' % (col_name + "idx", col_name))

# ## Automate addition of extra fields to database

def add_extras(gemini_db, chunk_dbs):
    """Annotate gemini database with extra columns from processed chunks, if available.
    """
    extra_files = []
    header_files = []
    for chunk in chunk_dbs:
        extra_file, header_file = get_extra_files(chunk)
        if os.path.exists(extra_file):
            extra_files.append(extra_file)
            assert os.path.exists(header_file)
            header_files.append(header_file)
    if header_files:
        header, types = _merge_headers(header_files)
        ops = ["first" for t in types]
        extra_beds = [_json_to_bed(x, header) for x in extra_files]
        final_bed = _merge_beds(extra_beds, gemini_db)
        Args = namedtuple("Args", "db,anno_file,anno_type,col_operations,col_names,col_types,col_extracts")
        args = Args(gemini_db, final_bed, "extract", ",".join(ops),
                    ",".join(header), ",".join(types),
                    ",".join([str(i + 4) for i in range(len(header))]))
        annotate(None, args)
        for fname in extra_beds + [final_bed, final_bed + ".tbi"] + header_files + extra_files:
            if os.path.exists(fname):
                os.remove(fname)

def _merge_beds(in_beds, final_db):
    """Merge BED files into a final sorted output file.
    """
    if len(in_beds) == 1:
        out_file = in_beds[0]
    else:
        out_file = "%s.bed" % os.path.splitext(final_db)[0]
        cmd = "cat %s | sort -k1,1 -k2,2n > %s" % (" ".join(in_beds), out_file)
        subprocess.check_call(cmd, shell=True)
    subprocess.check_call(["bgzip", "-f", out_file])
    bgzip_out = out_file + ".gz"
    subprocess.check_call(["tabix", "-p", "bed", "-f", bgzip_out])
    return bgzip_out

def _json_to_bed(fname, header):
    """Convert JSON output into a BED file in preparation for annotation.
    """
    out_file = "%s.bed" % os.path.splitext(fname)[0]
    with open(fname) as in_handle:
        with open(out_file, "w") as out_handle:
            for line in in_handle:
                cur_info = json.loads(line)
                parts = [str(cur_info.get(h, "")) for h in ["chrom", "start", "end"] + header]
                out_handle.write("\t".join(parts) + "\n")
    return out_file

def _merge_headers(header_files):
    """Merge a set of header files into a single final header for annotating.
    """
    ignore = set(["chrom", "start", "end"])
    ctype_order = ["text", "float", "integer", None]
    out = {}
    for h in header_files:
        with open(h) as in_handle:
            header = json.loads(in_handle.read())
            for column, ctype in header.items():
                if column not in ignore:
                    cur_ctype = sorted([ctype, out.get(column)], key=lambda x: ctype_order.index(x))[0]
                    out[column] = cur_ctype
    headers = []
    types = []
    for header in sorted(out.keys()):
        headers.append(header)
        types.append(out[header])
    return headers, types

def get_extra_files(gemini_db):
    """Retrieve extra file names associated with a gemini database, for flexible loading.
    """
    extra_file = "%s-extra.json" % os.path.splitext(gemini_db)[0]
    extraheader_file = "%s-extraheader.json" % os.path.splitext(gemini_db)[0]
    return extra_file, extraheader_file
