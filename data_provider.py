import pickle
import pandas as pd
from os.path import isfile
from structure_processor import *

PDBS = "data/pdbs/{0}.pdb"
DATASET_DESC_FILE = "data/dataset_desc.csv"
DATASET_MAX_CDR_LEN = 31
DATASET_MAX_AG_LEN = 1269
DATASET_PICKLE = "data.p"


def compute_entries():
    train_df = pd.read_csv(DATASET_DESC_FILE)
    max_cdr_len = DATASET_MAX_CDR_LEN
    max_ag_len = DATASET_MAX_AG_LEN

    num_in_contact = 0
    num_residues = 0

    all_cdrs = []
    all_lbls = []
    all_ags = []
    for _, entry in train_df.iterrows():
        print("Processing PDB: ", entry['PDB'])

        pdb_file = entry['PDB']
        ab_h_chain = entry['Ab Heavy Chain']
        ab_l_chain = entry['Ab Light Chain']
        ag_chain = entry['Ag']

        cdrs, ag, lbls, _, (nic, nr) =\
            open_single_pdb(pdb_file, ab_h_chain,
                            ab_l_chain, ag_chain,
                            max_ag_len=max_ag_len,
                            max_cdr_len=max_cdr_len)

        num_in_contact += nic
        num_residues += nr

        all_cdrs.append(cdrs)
        all_lbls.append(lbls)
        all_ags.append(ag)

    cdrs = np.stack(all_cdrs, axis=0)
    ag = np.stack(all_ags, axis=0)
    lbls = np.stack(all_lbls, axis=0)

    return (cdrs, ag, lbls,
            {"max_cdr_len": max_cdr_len, "max_ag_len": max_ag_len,
             "pos_class_weight": num_residues / num_in_contact})


def open_dataset():
    if isfile(DATASET_PICKLE):
        print("Precomputed dataset found, loading...")
        with open(DATASET_PICKLE, "rb") as f:
            dataset = pickle.load(f)
    else:
        print("Computing and storing the dataset...")
        dataset = compute_entries()
        with open(DATASET_PICKLE, "wb") as f:
            pickle.dump(dataset, f)

    return dataset


def neighbour_list_to_matrix(neigh_list):
    # Convert a list of tuples into a 2-element list of lists
    l = [list(t) for t in zip(*neigh_list)]
    # weights = 1 / np.array(l[0])
    residues = residue_seq_to_one(l[1])
    # Multiply each column by a vector element-wise
    return seq_to_feat_matrix(residues)# * weights[:, np.newaxis]


# def cdr_id_to_vector(cdr_name):
#     h_or_l = {'H': [0, 1], 'L': [1, 0]}
#     id = {'1': [0, 0, 1], '2': [0, 1, 0], '3': [1, 0, 0]}
#     return np.array(h_or_l[cdr_name[0]] + id[cdr_name[1]])


def open_single_pdb(pdb_file, ab_h_chain_id, ab_l_chain_id, ag_chain_id,
                    max_cdr_len, max_ag_len):
    parser = PDBParser()
    structure = parser.get_structure("", PDBS.format(pdb_file))

    # Extract CDRs and the antigen chain
    model = structure[0]

    cdrs = {}
    cdrs.update(extract_cdrs(model[ab_h_chain_id], ["H1", "H2", "H3"]))
    cdrs.update(extract_cdrs(model[ab_l_chain_id], ["L1", "L2", "L3"]))

    ag = model[ag_chain_id]

    # Compute ground truth -- contact information
    contact = [residue_in_contact_with_chains(res, cdrs.values()) for res in ag]
    num_residues = len(contact)
    num_in_contact = sum(contact)

    cdrs = {k: residue_seq_to_one(v) for k, v in cdrs.items()}

    # Convert to matrices
    cdr_mats = []
    for cdr_name in ["H1", "H2", "H3", "L1", "L2", "L3"]:
        cdr_chain = cdrs[cdr_name]

        # neigh_feats = [seq_to_feat_matrix(r) for r in cdr_chain]
        # cdr_mat = np.stack([m.flatten() for m in neigh_feats])

        cdr_mat = seq_to_feat_matrix(cdr_chain)
        cdr_mat_pad = np.zeros((max_cdr_len, NUM_CDR_FEATURES))
        cdr_mat_pad[:cdr_mat.shape[0], :] = cdr_mat
        cdr_mats.append(cdr_mat_pad)

    cdrs = np.stack(cdr_mats)

    ag_neighs = [neighbour_list_to_matrix(
                    residue_neighbourhood(r, ag, RESIDUE_NEIGHBOURS))
                 for r in ag.get_residues()]
    ag_mat = np.stack([feat.flatten() for feat in ag_neighs])
    ag_mat_pad = np.zeros((max_ag_len, NUM_AG_FEATURES))
    ag_mat_pad[:ag_mat.shape[0], :] = ag_mat

    cont_mat = np.array(contact, dtype=float)
    cont_mat_pad = np.zeros((max_ag_len, 1))
    cont_mat_pad[:cont_mat.shape[0], 0] = cont_mat

    return cdrs, ag_mat_pad, cont_mat_pad, structure, \
           (num_in_contact, num_residues)
