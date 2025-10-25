import torch
import triton
import triton.language as tl

@triton.jit
def _fwd_set_block_table_and_num_seq_alloc_blocks_kernel(
    block_table: torch.Tensor,		# [max_seqs_in_block_table, max_blocks_per_seq]
    candidate_blocks: torch.Tensor,	# [sum(block_needed)]
    seq_ids: torch.Tensor,			# [batch_size]
    num_seq_allocated_blocks: torch.Tensor, # [max_seqs_in_block_table]
    block_needed: torch.Tensor,		# [batch_size]
    block_needed_cumsum: torch.Tensor,	# [batch_size]
    max_blocks_per_seq: tl.constexpr
):
    # grid shape: [batch_size]
    my_batch_id = tl.program_id(0)
    my_seq_id = tl.load(seq_ids + my_batch_id).to(tl.int64)
    my_block_needed = tl.load(block_needed + my_batch_id)
    my_candidate_block_start_index = tl.load(block_needed_cumsum + my_batch_id) - my_block_needed
    my_num_allocated_blocks = tl.load(num_seq_allocated_blocks + my_seq_id)
    for i in range(my_block_needed):
        my_block_id = tl.load(candidate_blocks + my_candidate_block_start_index + i)
        tl.store(block_table + my_seq_id * max_blocks_per_seq + my_num_allocated_blocks + i, my_block_id)
    tl.store(num_seq_allocated_blocks + my_seq_id, my_num_allocated_blocks + my_block_needed)


# 宿主端 (host-side) 封装函数，即它在 CPU 端准备好数据并启动 Triton GPU kernel 去执行真正的工作。
def set_block_table_and_num_seq_alloc_blocks(
    num_seq_allocated_blocks: torch.Tensor, # [max_seqs_in_block_table]
    block_table: torch.Tensor,        # [max_seqs_in_block_table, max_blocks_per_seq]
    candidate_blocks: torch.Tensor,   # [sum(block_needed)]
    seq_ids: torch.Tensor,            # [batch_size]
    block_needed: torch.Tensor,       # [batch_size]
):
    """
    Set block_table and num_seq_allocated_blocks

    For the ith sequence in the batch which has seq_id s:
    - Set block_table[s][num_seq_allocated_block[s]: num_seq_allocated_block[s] + block_needed[i]] to
      candidate_blocks[block_needed_cumsum[i-1]: block_needed_cumsum[i]]
    - Set num_seq_allocated_blocks[s] to num_seq_allocated_blocks[s] + block_needed[i]
    """
    block_needed_cumsum = torch.cumsum(block_needed, 0)
    max_blocks_per_seq = block_table.shape[1]
    grid = (block_needed.shape[0], )
    _fwd_set_block_table_and_num_seq_alloc_blocks_kernel[grid](
        block_table, candidate_blocks, seq_ids, num_seq_allocated_blocks, block_needed, block_needed_cumsum, max_blocks_per_seq
    )


@triton.jit
def _fwd_unset_block_table_and_num_seq_alloc_blocks_kernel(
    num_seq_allocated_blocks: torch.Tensor,	# [max_seqs_in_block_table]
    block_table: torch.Tensor,		    # [max_seqs_in_block_table, max_blocks_per_seq]
    seq_ids: torch.Tensor,			    # [batch_size]
    is_block_free: torch.Tensor,		# [num_blocks], bool
    max_blocks_per_seq: tl.constexpr
):
    # grid shape: [batch_size]
    my_batch_id = tl.program_id(0)
    my_seq_id = tl.load(seq_ids + my_batch_id)
    my_num_blocks = tl.load(num_seq_allocated_blocks + my_seq_id)
    for i in range(my_num_blocks):
        my_block_id = tl.load(block_table + my_seq_id * max_blocks_per_seq + i)
        tl.store(is_block_free + my_block_id, True)
    tl.store(num_seq_allocated_blocks + my_seq_id, 0)

def unset_block_table_and_num_seq_alloc_blocks(
    num_seq_allocated_blocks: torch.Tensor,	# [max_seqs_in_block_table]
    block_table: torch.Tensor,		        # [max_seqs_in_block_table, max_blocks_per_seq]
    seq_ids: torch.Tensor,			        # [batch_size]
    is_block_free: torch.Tensor,		    # [num_blocks], bool
):
    """
    Mark the blocks allocated for the specified sequences in the `is_block_free`
    as free, and set corresponding num_seq_allocated_blocks to 0
    """
    max_blocks_per_seq = block_table.shape[1]
    grid = (seq_ids.shape[0], )
    _fwd_unset_block_table_and_num_seq_alloc_blocks_kernel[grid](
        num_seq_allocated_blocks, block_table, seq_ids, is_block_free, max_blocks_per_seq
    )


@triton.jit
def _fwd_gather_allocated_blocks_and_unset_kernel(
    num_seq_allocated_blocks: torch.Tensor,	# [max_seqs_in_block_table]
    block_table: torch.Tensor,		    # [max_seqs_in_block_table, max_blocks_per_seq]
    seq_ids: torch.Tensor,			    # [batch_size]
    is_block_free: torch.Tensor,		# [num_blocks], bool

    num_allocated_blocks_cumsum: torch.Tensor, # [batch_size]
    gathered_block_ids: torch.Tensor, # [sum(num_seq_allocated_blocks[seq_ids])]

    max_blocks_per_seq: tl.constexpr
):
    # grid shape: [batch_size]
    my_batch_id = tl.program_id(0)
    my_seq_id = tl.load(seq_ids + my_batch_id)
    my_num_blocks = tl.load(num_seq_allocated_blocks + my_seq_id)
    my_num_allocated_blocks_cumsum = tl.load(num_allocated_blocks_cumsum+my_batch_id-1, mask=my_batch_id>0, other=0)
    for i in range(my_num_blocks):
        my_block_id = tl.load(block_table + my_seq_id * max_blocks_per_seq + i)
        tl.store(gathered_block_ids + my_num_allocated_blocks_cumsum + i, my_block_id)
        tl.store(is_block_free + my_block_id, True)
    tl.store(num_seq_allocated_blocks + my_seq_id, 0)

def gather_allocated_blocks_and_unset(
    num_seq_allocated_blocks: torch.Tensor,	# [max_seqs_in_block_table]
    block_table: torch.Tensor,		        # [max_seqs_in_block_table, max_blocks_per_seq]
    seq_ids: torch.Tensor,			        # [batch_size]
    is_block_free: torch.Tensor,		    # [num_blocks], bool
) -> torch.Tensor:
    """
    Gather the block IDs allocated for the specified sequences and mark them as free
    """
    if seq_ids.numel() == 0:
        return torch.empty((0,), dtype=torch.int32, device=block_table.device)
    num_allocated_blocks_cumsum = torch.cumsum(num_seq_allocated_blocks[seq_ids], 0)
    gathered_block_ids = torch.empty((num_allocated_blocks_cumsum[-1].item(),), dtype=torch.int32, device=block_table.device)

    max_blocks_per_seq = block_table.shape[1]
    grid = (seq_ids.shape[0], )
    _fwd_gather_allocated_blocks_and_unset_kernel[grid](
        num_seq_allocated_blocks, block_table, seq_ids, is_block_free,
        num_allocated_blocks_cumsum, gathered_block_ids, max_blocks_per_seq
    )

    return gathered_block_ids
