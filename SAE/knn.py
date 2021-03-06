from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os
import pdb
import glob
import numpy as np
from numpy import linalg as LA
from scipy.sparse import csr_matrix
import scipy.sparse as ss
from numpy.linalg import inv

def similarity(x_img,lamuda):
    """
    [image_feature, image_index] = similarity(x_img,lamuda)
    create the knn lists and feature based on the 4-neighboors pixels
    x_img: M*M images
    lamuda: parameter to control how much the neighboors pixels influence the center pixel
    image_feature: the k*N list of corresponding similarities for knn
    image_index: k*N list of knn per vector (used to build the knn graph)
    """
    shape = x_img.shape
    im_m = x_img
    im_mod = np.pad(np.array(x_img,dtype=float), ((1,1),(1,1)),'constant',constant_values=np.inf)
    im_r = im_mod[0:shape[0]  ,1:shape[1]+1]
    im_l = im_mod[2:shape[0]+2,1:shape[1]+1]
    im_u = im_mod[1:shape[0]+1,0:shape[1]]
    im_d = im_mod[1:shape[0]+1,2:shape[1]+2]
    im_r = np.exp(-lamuda*np.square(im_r-im_m))
    im_l = np.exp(-lamuda*np.square(im_l-im_m))
    im_u = np.exp(-lamuda*np.square(im_u-im_m))
    im_d = np.exp(-lamuda*np.square(im_d-im_m))
    im_r = np.reshape(im_r,(1,shape[0]*shape[1]))
    im_l = np.reshape(im_l,(1,shape[0]*shape[1]))
    im_u = np.reshape(im_u,(1,shape[0]*shape[1]))
    im_d = np.reshape(im_d,(1,shape[0]*shape[1]))
    image_feature = np.concatenate((im_r,im_l,im_u,im_d),axis=0)

    image_index = (shape[0]*shape[1]+1)*np.ones((4,shape[0]*shape[1]+1),dtype=int)
    ind = np.arange(shape[0]*shape[1]).reshape(shape[0],shape[1])
    ind_mod = np.pad(np.array(ind,dtype=int),((1,1),(1,1)),'constant',constant_values = shape[0]*shape[1]+1)
    ind_r = ind_mod[0:shape[0]  ,1:shape[1]+1].reshape(1,shape[0]*shape[1])
    ind_l = ind_mod[2:shape[0]+2,1:shape[1]+1].reshape(1,shape[0]*shape[1])
    ind_u = ind_mod[1:shape[0]+1,0:shape[1]].reshape(1,shape[0]*shape[1])
    ind_d = ind_mod[1:shape[0]+1,2:shape[1]+2].reshape(1,shape[0]*shape[1])
    image_index[:,0:shape[0]*shape[1]] = np.concatenate((ind_r,ind_l,ind_u,ind_d),axis=0)

    return image_feature, image_index

def knngraph(knn_ind,sim_fea):
    """
    A = knngraph(knn_ind, sim_fea)
    create the affinity matrix for the mutual kNN graph based on the knn lists
    knn_ind: kxN list of knn per vector
    sim_fea: kxN list of corresponding similarities for knn
    A: sparse affinity matrix NxN
    """
    shape = knn_ind.shape
    sim_fea[sim_fea<0] = 0 # similarity should be non-negative

    data = np.array([],dtype=float)
    row  = np.array([],dtype=int)
    col  = np.array([],dtype=int)
    for i in range(shape[1]):
        tmp = knn_ind[:,i:i+1]
        mem = (tmp<shape[1])
        nk  = np.sum(mem+0)
        if nk >0:
            row = np.append(row,i*np.ones((nk,)))
            col = np.append(col,tmp[mem])
            tp_v = sim_fea[:,i:i+1]
            data = np.append(data,tp_v[mem])
    A=csr_matrix((data,(row,col)),shape=(shape[1]-1,shape[1]-1))
    A.setdiag(0) # diagonal to 0
    return A

def transition_matrix(W):
    """
    S =  transition_matrix(W)
    calculate the transition matrix of W
    W: N*N sparse affinity matrix
    S: N*N sparse transition matrix
    """
    shape = W.shape
    tmp = W.sum(1)
    tmp = np.power(tmp,-0.5)
    D = ss.spdiags(tmp.transpose(),np.array([0]),shape[0],shape[1])
    S = D * W * D
    return S

def graph_matrix(Aa,index):
    """
    fea = graph_matrix(Aa, index)
    transfer N*N sparse matrix to k*N matrix
    Aa: N*N smoothness matrix
    image_index: M*M image index, use to transfer the Aa to k*N matrix
    fea: k*N matrix
    """
    shape = Aa.shape
    fea = np.zeros((4,shape[0]))
    for i in range(shape[0]):
        for j in range(4):
            if index[j,i]<=shape[0]:
                fea[j,i] = Aa[i,index[j,i]]
    return fea

def construct_sparse(img,lamuda):
    """
    S, image_index = construct_sparse(img,lamuda)
    construct sparse matrix
    img: M*M image, one change of images for RGB images
    lamuda: parameter for similarity
    S: N*N transation matrix matrix
    image_index: M*ddM image index, use to transfer the Aa to k*N matrix
    """
    image_feature,image_index = similarity(img,lamuda)
    A_ = knngraph(image_index,image_feature)
    S  = transition_matrix(A_)
    return S, image_index

def construct_imagenet_graph(imgs,lamuda,alpha):
    """
    A = construct_imagenet_graph(img,lamuda,alpha)
    construct sparse matrix for imagenet images (RGB images)
    img: M*M*3 image
    lamuda: parameter for similarity
    alpha: parameter to control the smoothness
    A: k*M*M*3 smoothness matrix
    """
    shape=imgs.shape
    Ha = np.ones((shape[0],4,shape[1],shape[2],shape[3]))
    for img_i in range(shape[0]):
        img = imgs[img_i]
        for dim_i in range(shape[3]):
            img_c = np.array(img[:,:,dim_i],dtype=np.float32)
            S, ind = construct_sparse(img_c, lamuda)
            Aa = ss.eye(shape[2]*shape[1]) - alpha * S
            Aa = csr_matrix.todense(Aa)
            A = graph_matrix(Aa,ind)
            A = np.reshape(A,(4,shape[1],shape[2]))
            Ha[img_i,:,:,:,dim_i] = A
    return Ha

def construct_mnist_graph(imgs,lamuda,alpha):
    """
    pi,pit = construct_mnist_graph(img,lamuda,alpha,eig_num)
    construct sparse matrix for mnist images (gray images)
    img: M*M image
    lamuda: parameter for similarity
    alpha: parameter to control the smoothness
    """
    shape=imgs.shape
    Aa = np.zeros((shape[0],shape[1]*shape[2],shape[1]*shape[2]))
    for i in range(shape[0]):
        img = np.reshape(imgs[i],(shape[1],shape[2]))
        S, ind = construct_sparse(img, lamuda)
        # calculate the 
        A = ss.eye(shape[1]*shape[2]) - alpha * S
        A = csr_matrix.todense(A)
        A = inv(A)
        A = (1-alpha)*A
        z = np.sum(A,axis=0)
        Aa[i] = A / z
    return Aa

