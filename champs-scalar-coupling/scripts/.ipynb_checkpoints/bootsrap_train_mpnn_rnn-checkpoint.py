#############################################################################################################
#                                                                                                           #
#                                              helper functions                                             #
#                                                                                                           #
#############################################################################################################

def build_test_data(fold, grm, coupling_frame, molecule_edge, molecule_node):
    
    # Transform test predictions to gaussrank 
    df_test = coupling_frame[['coupling_type', 'scalar_coupling_constant']]
    df_test.columns = ['type', 'scalar_coupling_constant']

    # Reverse type mapping 
    df_test.type = df_test.type.map(REVERSE_COUPLING_TYPE)

    #fit grm 
    t0 = time()
    transformed_test = grm.convert_df(df_test, from_coupling=True)
    coupling_frame['gaussrank_coupling'] =  transformed_test
    print('\nGetting gaussrank transformation for test data took %s seconds\n' %(time()-t0))
    
    # Build molecule coupling frame for fold 
    coupling_cols = ['atom_index_0', 'atom_index_1', 'coupling_type', 'scalar_coupling_constant', 'gaussrank_coupling',
                    'fc', 'sd', 'pso', 'dso', 'id',
                    'path_index_0', 'path_index_1', 'path_index_2','path_index_3', 
                    'path_btype_0', 'path_btype_1', 'path_btype_2',
                    'path_a_num_0', 'path_a_num_1', 'path_a_num_2', 'path_a_num_3']

    shared_cols = ['molecule_name', 'num_coupling', 'coupling_dim']

    tmp = coupling_frame.groupby('molecule_name').apply(lambda x: x[coupling_cols].values.reshape(-1))
    molecule_coupling = pd.DataFrame(tmp.values.tolist())
    # pad coupling_max from 132 to 136
    COUPLING_MAX = 136
    pad_cols = 21*5
    d = dict.fromkeys([str(i) for i in range(molecule_coupling.shape[1], molecule_coupling.shape[1]+pad_cols)], 0.0)
    molecule_coupling = molecule_coupling.assign(**d).fillna(0.0)
    molecule_coupling['molecule_name'] = tmp.index
    molecule_coupling = molecule_coupling.merge(coupling_frame[shared_cols].drop_duplicates(), on='molecule_name', how='left')
    cols = molecule_coupling.columns.tolist()

    new_cols = cols[-3:] + cols[:-3]
    molecule_coupling = molecule_coupling[new_cols]
    molecule_coupling.columns = ['molecule_name', 'num_coupling', 'coupling_dim'] + ['coupling_%s'%i for i in range(COUPLING_MAX*21)]

    node_edge_frame = pd.merge(molecule_node, molecule_edge, on='molecule_name', how='left')
    general_stack_frame =  pd.merge(node_edge_frame, molecule_coupling, on='molecule_name', how='left')
    return general_stack_frame

#############################################################################################################
#                                                                                                           #
#                               Run a training process for model: MPNN+RNN                                  #
#                                                                                                           #
#############################################################################################################


def run_train(yaml_filepath, fold, best_pred_file_path):
    
    '''
    Run training for specific fold 
    
    Arguments: path to config file of the experiment 
    
    Saves : 
        - Train log file 
        - test predictions gzip file 
        - cv predictions gzip file 
    '''
    cfg = load_cfg(yaml_filepath)
    
    ############################------------- Set Train flags ---------------################################
    num_output = cfg['model']['regression']['num_output']
    OUT_DIR = cfg['dataset']['output_path']
    if num_output == 1:
        out_dir = OUT_DIR + '/submit/scalar_output/'
        # init preditions arrays 
        pred_cv = np.zeros( cfg['train']['train_shape'])
        pred_sub = np.zeros(cfg['train']['test_shape'])
    
    elif num_output == 5:
        out_dir = OUT_DIR + '/submit/multi_output/'
        pred_cv = np.zeros((cfg['train']['train_shape'], 5))
        pred_sub = np.zeros((cfg['train']['test_shape'], 5))
        
    DATA_DIR = cfg['dataset']['input_path']
    normalize = cfg['dataset']['normalize']
    gaussrank=  cfg['dataset']['gaussrank']
    COUPLING_MAX = 136
    model_name = cfg['train']['model_name']  
    model_name = model_name+ '_fold_%s' %fold 
    batch_size = cfg['train']['batch_size']
    predict_type = cfg['train']['predict_type']
    loss_name = cfg['train']['loss_name']
    predict_type = cfg['model']['regression']['predict_type']
    epochs = cfg['train']['epochs']
    max_lr = cfg['train']['max_lr']
    device = cfg['train']['device']
    y_range=cfg['model']['y_range']
    
    ############################------------- Init Log file ---------------################################
    log = Logger()
    log.open(out_dir+'/train/log.train.%s.%s.txt' % (cfg['train']['model_name'], fold), mode='a')
    log.write('\n--- [START %s] %s\n\n' % (IDENTIFIER, '-' * 64))
    log.write('\t%s\n' % COMMON_STRING)
    log.write('\n')

    log.write('\tSEED         = %u\n' % SEED)
    log.write('\tPROJECT_PATH = %s\n' % PROJECT_PATH)
    log.write('\t__file__     = %s\n' % __file__)
    log.write('\tout_dir      = %s\n' % out_dir)
    log.write('\tconfig file  = %s\n ' % yaml_filepath)
    log.write('\n')
    
    
    ############################----------- Load GRM transformer -------------################################
    log.write('\n Load GaussRank mapping for fold %s' %fold)
    data_dir = DATA_DIR + '/rnn_parquet'
    files = glob.glob(data_dir+'/fold_%s/'%fold+'*.csv')
    mapping_frames = ['']*8
    coupling_order = ['']*8

    for file in files:
        type_ = file.split('/')[-1].split('_')[2]
        order = int(file.split('/')[-1].split('_')[-1].strip('.csv'))
        coupling_order[order] = type_
        mapping_frames[order] = pd.read_csv(file)  
    grm = GaussRankMap(mapping_frames, coupling_order)
    
    
    ############################------------- Load Datasets ---------------################################
    log.write('** dataset setting **\n')
    
    COUPLING_MAX = 136
    
    log.write('\nBuild test features for fold %s' %fold)
    test= pd.read_csv(DATA_DIR+'/csv/test.csv')
    id_test = test.id.values
    mol_test = test.molecule_name.values
    molecule_edge = pd.read_parquet(DATA_DIR+'/parquet/molecule_edge.parquet')
    molecule_edge = molecule_edge[molecule_edge.molecule_name.isin(mol_test)]
    molecule_node = pd.read_parquet(DATA_DIR+'/parquet/molecule_node.parquet')
    molecule_node = molecule_node[molecule_node.molecule_name.isin(mol_test)]
    coupling_frame = pd.read_parquet(DATA_DIR+'/parquet/baseline_rnn_coupling_frame.parquet')
    coupling_frame = coupling_frame[coupling_frame.molecule_name.isin(mol_test)]

    with gzip.open(best_pred_file_path) as f:
        best_stack_test = pd.read_csv(f) 
        
    coupling_frame = coupling_frame.merge(best_stack_test, on = 'id', how='left')
    test_frame = build_test_data(fold, grm, coupling_frame, molecule_edge, molecule_node)
    
    log.write('\n Load Train/Validation features for fold %s' %fold)
    validation = gd.read_parquet(DATA_DIR +'/rnn_parquet/fold_%s/validation.parquet'%fold)
    train = gd.read_parquet(DATA_DIR +'/rnn_parquet/fold_%s/train.parquet' %fold)
    
    # transform pandas to cudf 
    log.write('\n Define new train with test observations' )
    test = gd.from_pandas(test_frame)
    train  = gd.concat([train, test])
    del test

    # convert tensors
    log.write('** Convert train tensors **\n')
    num_nodes_tensor = from_dlpack(train['num_nodes'].to_dlpack()).long()
    num_edges_tensor = from_dlpack(train['num_edge'].to_dlpack()).long()
    num_coupling_tensor = from_dlpack(train['num_coupling'].to_dlpack()).long()
    node_cols = [i for i in train.columns if re.compile("^node_[0-9]+").findall(i)]
    nodes_matrix = from_dlpack(train[node_cols].to_dlpack()).type(torch.float32)
    edge_cols = [i for i in train.columns if re.compile("^edge_[0-9]+").findall(i)]
    edges_matrix = from_dlpack(train[edge_cols].to_dlpack()).type(torch.float32)
    coupling_cols = [i for i in train.columns if re.compile("^coupling_[0-9]+").findall(i)]
    coupling_matrix = from_dlpack(train[coupling_cols].to_dlpack()).type(torch.float32)
    mol_train = train.molecule_name.unique().to_pandas().values
    train_dataset = TensorBatchDataset(mol_train, 
                                    tensors=[nodes_matrix, edges_matrix, coupling_matrix,
                                            num_nodes_tensor, num_edges_tensor, num_coupling_tensor], 
                                    batch_size=batch_size,
                                    collate_fn=tensor_collate_rnn,
                                    COUPLING_MAX=COUPLING_MAX,
                                    mode='train',
                                    csv='train')
    del train
    # convert validation to tensors 
    log.write('** Convert validation tensors **\n')
    num_nodes_tensor = from_dlpack(validation['num_nodes'].to_dlpack()).long()
    num_edges_tensor = from_dlpack(validation['num_edge'].to_dlpack()).long()
    num_coupling_tensor = from_dlpack(validation['num_coupling'].to_dlpack()).long()
    node_cols = [i for i in validation.columns if re.compile("^node_[0-9]+").findall(i)]
    nodes_matrix = from_dlpack(validation[node_cols].to_dlpack()).type(torch.float32)
    edge_cols = [i for i in validation.columns if re.compile("^edge_[0-9]+").findall(i)]
    edges_matrix = from_dlpack(validation[edge_cols].to_dlpack()).type(torch.float32)
    coupling_cols = [i for i in validation.columns if re.compile("^coupling_[0-9]+").findall(i)]
    coupling_matrix = from_dlpack(validation[coupling_cols].to_dlpack()).type(torch.float32)
    mol_valid = validation.molecule_name.unique().to_pandas().values
    valid_dataset = TensorBatchDataset(mol_valid, 
                                    tensors=[nodes_matrix, edges_matrix, coupling_matrix,
                                                num_nodes_tensor, num_edges_tensor, num_coupling_tensor], 
                                    batch_size=batch_size,
                                    collate_fn=tensor_collate_rnn,
                                    COUPLING_MAX=COUPLING_MAX,
                                    mode='train',
                                    csv='train')
    del validation 
    ### log dataset info
    log.write('batch_size = %d\n'%(batch_size))
    log.write('train_dataset : \n%s\n'%(train_dataset))
    log.write('valid_dataset : \n%s\n'%(valid_dataset))
    log.write('\n')
    
    data = BatchDataBunch.create(train_dataset, valid_dataset, device=device, bs=batch_size)
    
    ############################------------- Fastai Learner ---------------################################
    log.write('** net setting **\n')
    #### Init Fastai learner 
    net = Net(cfg, y_range=y_range)
    log.write('\tCriterion: %s\n'%(loss_name))

    optal = partial(RAdam)

    learn =  Learner(data,
                     net.cuda(),
                     metrics=None,
                     opt_func=optal,
                     callback_fns=partial(LMAE,
                                        grm=grm,
                                        predict_type=predict_type,
                                        normalize_coupling=normalize,
                                        coupling_rank=gaussrank))

    learn.loss_func = partial(train_criterion, 
                              criterion=loss_name,
                              num_output=num_output,
                              gaussrank=gaussrank,
                              pred_type=predict_type) 

    log.write('\tTraining loss: %s\n'%(learn.loss_func))
    log.write('\tfit one cycle of length: %s\n'%epochs)
    learn.fit_one_cycle(epochs,
                        max_lr, 
                        callbacks=[SaveModelCallback(learn,
                                                 every='improvement',
                                                 monitor='LMAE', 
                                                 name=cfg['train']['model_name']+'_fold_%s'%fold,
                                                 mode='min')])
    log.write('\nGet Validation loader\n')
    valid_dataset = TensorBatchDataset(mol_valid, 
                                tensors=[nodes_matrix, edges_matrix, coupling_matrix,
                                        num_nodes_tensor, num_edges_tensor, num_coupling_tensor], 
                                batch_size=batch_size,
                                collate_fn=tensor_collate_rnn,
                                COUPLING_MAX=COUPLING_MAX,
                                mode='test',
                                csv='train')

    valid_loader = BatchDataLoader(valid_dataset, 
                                   shuffle=False, 
                                   pin_memory=False, 
                                   drop_last=False, 
                                   device=device)

    valid_dataset.get_total_samples()
    log.write('\n Compute predictions for validation data at fold %s\n' %fold)
    valid_loss, reverse_frame, contributions, molecule_representation = do_test(learn.model,
                                                                           valid_loader,
                                                                           valid_dataset.total_samples,
                                                                           1,
                                                                           predict_type,
                                                                           grm,
                                                                           normalize=normalize,
                                                                           gaussrank=gaussrank)

    print('\n')
    print('|------------------------------------ VALID ------------------------------------------------|\n')
    print('| 1JHC,   2JHC,   3JHC,   1JHN,   2JHN,   3JHN,   2JHH,   3JHH  |  loss  mae log_mae | fold |\n')
    print('|-------------------------------------------------------------------------------------------|\n')
    print('|%+0.3f, %+0.3f, %+0.3f, %+0.3f, %+0.3f, %+0.3f, %+0.3f, %+0.3f | %+5.3f %5.2f %+0.2f |  %s   |\n' %(*valid_loss[:11], fold))
    
    log.write('\n|%+0.3f, %+0.3f, %+0.3f, %+0.3f, %+0.3f, %+0.3f, %+0.3f, %+0.3f | %+5.3f %5.2f %+0.2f |  %s   |\n' %(*valid_loss[:11], fold))
    
    log.write('\nSave model to disk')
    torch.save(learn.model, 'models/' + cfg['train']['model_name'] + '_fold_%s_final_save.pth'%fold)
    
    del nodes_matrix
    del edges_matrix
    del coupling_matrix 
    torch.cuda.empty_cache()
    
    
    log.write('load test data')
    test = gd.read_parquet(DATA_DIR +'/rnn_parquet/test.parquet')
    num_nodes_tensor = from_dlpack(test['num_nodes'].to_dlpack())
    num_edges_tensor = from_dlpack(test['num_edge'].to_dlpack())
    num_coupling_tensor = from_dlpack(test['num_coupling'].to_dlpack())
    node_cols = [i for i in test.columns if re.compile("^node_[0-9]+").findall(i)]
    nodes_matrix = from_dlpack(test[node_cols].to_dlpack())
    nodes_matrix = from_dlpack(test[node_cols].to_dlpack()).type(torch.float32)
    edge_cols = [i for i in test.columns if re.compile("^edge_[0-9]+").findall(i)]
    edges_matrix = from_dlpack(test[edge_cols].to_dlpack()).type(torch.float32)
    coupling_cols = [i for i in test.columns if re.compile("^coupling_[0-9]+").findall(i)]
    coupling_matrix = from_dlpack(test[coupling_cols].to_dlpack()).type(torch.float32)

    mol_test  = test.molecule_name.unique().to_pandas().values
    del test

    test_dataset = TensorBatchDataset(mol_test, 
                                    tensors=[nodes_matrix, edges_matrix, coupling_matrix,
                                             num_nodes_tensor, num_edges_tensor, num_coupling_tensor], 
                                    batch_size=batch_size,
                                    collate_fn=tensor_collate_rnn,
                                    COUPLING_MAX=COUPLING_MAX,
                                    mode='test',
                                    csv='test')

    test_loader = BatchDataLoader(test_dataset, 
                                   shuffle=False, 
                                   pin_memory=False, 
                                   drop_last=False, 
                                   device='cuda')

    log.write('\n Compute predictions for test data at fold %s\n' %fold)
    test_loss, preds_fold_test, contributions, molecule_representation = do_test(learn.model,
                                                                           valid_loader,
                                                                           cfg['train']['test_shape'], 
                                                                           1,
                                                                           predict_type,
                                                                           grm,
                                                                           normalize=normalize,
                                                                           gaussrank=gaussrank)
    log.write('\n Save predictions to disk')
    val_loss = valid_loss[-1]
    log.write('\n Save Validation frame' )
    clock = "{}".format(datetime.now()).replace(' ','-').replace(':','-').split('.')[0]
    output_name = out_dir + '/cv_%s_%s_%.4f_fold_%s.csv.gz'%(clock, loss_name, val_loss, fold)
    reverse_frame.to_csv(output_name, index=False,compression='gzip')
    
    # save test predictions 
    log.write('\n Save Test frame' )
    clock = "{}".format(datetime.now()).replace(' ','-').replace(':','-').split('.')[0]
    output_name = out_dir + '/sub_%s_%s_%.4f_fold_%s.csv.gz'%(clock, loss_name, val_loss, fold)
    preds_fold_test.to_csv(output_name, index=False,compression='gzip')

def get_parser():
    """Get parser object."""
    parser = ArgumentParser(description=__doc__,
                            formatter_class=ArgumentDefaultsHelpFormatter)
    
    parser.add_argument("-f", "--file",
                        dest="filename",
                        help="experiment definition file",
                        metavar="FILE",
                        required=True)
    
    
    parser.add_argument('--fold',  type=int, help='fold id for cv training', required=True)
        
    parser.add_argument('--GPU_id',  type=int, help='gpu to use for training', required=True)
    
    parser.add_argument('--best_pred_file',  type=str, help='path to best prediction file (csv.gz)', required=False)
    
    return parser

#############################################################################################################
#                                                                                                           #
#                                               Main function                                               #
#                                                                                                           #
#############################################################################################################
if __name__ == '__main__':
    
    from argparse import ArgumentParser, ArgumentDefaultsHelpFormatter
    args = get_parser().parse_args()
    
    import os
    os.environ['CUDA_VISIBLE_DEVICES'] = str(args.GPU_id)
    
    import sys

    from os.path import dirname, join, abspath
    sys.path.insert(0, abspath(join(dirname(__file__), '..')))

    import cudf as gd
    from fastai.basic_train import *
    from fastai.callbacks import SaveModelCallback
    from functools import partial
    from torch.utils.dlpack import from_dlpack
    
    import glob 
    import gzip
    import warnings
    
    from mpnn_model.build_predictions import do_test 
    from mpnn_model.callback import get_reverse_frame, lmae, LMAE
    from mpnn_model.common import * 
    from mpnn_model.common_constants import * 
    from mpnn_model.dataset import TensorBatchDataset, BatchDataBunch, BatchDataLoader
    from mpnn_model.data_collate import tensor_collate_rnn
    from mpnn_model.GaussRank import GaussRankMap
    from mpnn_model.helpers import load_cfg
    from mpnn_model.model import Net 
    from mpnn_model.radam import * 
    from mpnn_model.train_loss import train_criterion, lmae_criterion
    
    from time import time
    
    print( '%s: calling main function ... ' % os.path.basename(__file__))    
    
    run_train(args.filename, args.fold, args.best_pred_file)

    print('\nsuccess!')
  