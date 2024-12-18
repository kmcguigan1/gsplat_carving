import struct
from typing import Optional, Tuple
from typing_extensions import Literal, assert_never

import torch
import torch.nn.functional as F
from torch import Tensor
from einops import rearrange
import gc
import math
import os

def _quat_to_rotmat(quats: Tensor) -> Tensor:
    """Convert quaternion to rotation matrix."""
    quats = F.normalize(quats, p=2, dim=-1)
    w, x, y, z = torch.unbind(quats, dim=-1)
    R = torch.stack(
        [
            1 - 2 * (y**2 + z**2),
            2 * (x * y - w * z),
            2 * (x * z + w * y),
            2 * (x * y + w * z),
            1 - 2 * (x**2 + z**2),
            2 * (y * z - w * x),
            2 * (x * z - w * y),
            2 * (y * z + w * x),
            1 - 2 * (x**2 + y**2),
        ],
        dim=-1,
    )
    return R.reshape(quats.shape[:-1] + (3, 3))


def _quat_scale_to_matrix(
    quats: Tensor,  # [N, 4],
    scales: Tensor,  # [N, 3],
) -> Tensor:
    """Convert quaternion and scale to a 3x3 matrix (R * S)."""
    R = _quat_to_rotmat(quats)  # (..., 3, 3)
    M = R * scales[..., None, :]  # (..., 3, 3)
    return M


def _quat_scale_to_covar_preci(
    quats: Tensor,  # [N, 4],
    scales: Tensor,  # [N, 3],
    compute_covar: bool = True,
    compute_preci: bool = True,
    triu: bool = False,
) -> Tuple[Optional[Tensor], Optional[Tensor]]:
    """PyTorch implementation of `gsplat.cuda._wrapper.quat_scale_to_covar_preci()`."""
    R = _quat_to_rotmat(quats)  # (..., 3, 3)

    if compute_covar:
        M = R * scales[..., None, :]  # (..., 3, 3)
        covars = torch.bmm(M, M.transpose(-1, -2))  # (..., 3, 3)
        if triu:
            covars = covars.reshape(covars.shape[:-2] + (9,))  # (..., 9)
            covars = (
                covars[..., [0, 1, 2, 4, 5, 8]] + covars[..., [0, 3, 6, 4, 7, 8]]
            ) / 2.0  # (..., 6)
    if compute_preci:
        P = R * (1 / scales[..., None, :])  # (..., 3, 3)
        precis = torch.bmm(P, P.transpose(-1, -2))  # (..., 3, 3)
        if triu:
            precis = precis.reshape(precis.shape[:-2] + (9,))
            precis = (
                precis[..., [0, 1, 2, 4, 5, 8]] + precis[..., [0, 3, 6, 4, 7, 8]]
            ) / 2.0

    return covars if compute_covar else None, precis if compute_preci else None

# Chane to give the 3d covariance
def _persp_proj(
    means: Tensor,  # [C, N, 3]
    covars: Tensor,  # [C, N, 3, 3]
    Ks: Tensor,  # [C, 3, 3]
    width: int,
    height: int,
) -> Tuple[Tensor, Tensor]:
    """PyTorch implementation of perspective projection for 3D Gaussians.

    Args:
        means: Gaussian means in camera coordinate system. [C, N, 3].
        covars: Gaussian covariances in camera coordinate system. [C, N, 3, 3].
        Ks: Camera intrinsics. [C, 3, 3].
        width: Image width.
        height: Image height.

    Returns:
        A tuple:

        - **means2d**: Projected means. [C, N, 2].
        - **cov3d_project**: Projected covariances. [C, N, 3, 3].
        - **l**: Returns the depth of each point projected coordinates. 
    """
    
    C, N, _ = means.shape

    tx, ty, tz = torch.unbind(means, dim=-1)  # [C, N]
    tz2 = tz**2  # [C, N]

    fx = Ks[..., 0, 0, None]  # [C, 1]
    fy = Ks[..., 1, 1, None]  # [C, 1]
    cx = Ks[..., 0, 2, None]  # [C, 1]
    cy = Ks[..., 1, 2, None]  # [C, 1]
    tan_fovx = 0.5 * width / fx  # [C, 1]
    tan_fovy = 0.5 * height / fy  # [C, 1]

    lim_x_pos = (width - cx) / fx + 0.3 * tan_fovx
    lim_x_neg = cx / fx + 0.3 * tan_fovx
    lim_y_pos = (height - cy) / fy + 0.3 * tan_fovy
    lim_y_neg = cy / fy + 0.3 * tan_fovy
    tx = tz * torch.clamp(tx / tz, min=-lim_x_neg, max=lim_x_pos)
    ty = tz * torch.clamp(ty / tz, min=-lim_y_neg, max=lim_y_pos)

    l = torch.sqrt(tx**2 + ty**2 + tz2)

    O = torch.zeros((C, N), device=means.device, dtype=means.dtype)
    J = torch.stack(
        [fx / tz, O, -fx * tx / tz2, O, fy / tz, -fy * ty / tz2, tx/l, ty/l, tz/l ], dim=-1
    ).reshape(C, N, 3, 3)
    
    # Get the projected 3d covariance
    cov3d_projected = torch.einsum("...ij,...jk,...kl->...il", J, covars, J.transpose(-1, -2))

    # Project the means
    means2d = torch.einsum("cij,cnj->cni", Ks[:, :2, :3], means)  # [C, N, 2]
    means2d = means2d / tz[..., None]  # [C, N, 2]
    return means2d, cov3d_projected, l  # [C, N, 2], [C, N, 2, 2]


def _fisheye_proj(
    means: Tensor,  # [C, N, 3]
    covars: Tensor,  # [C, N, 3, 3]
    Ks: Tensor,  # [C, 3, 3]
    width: int,
    height: int,
) -> Tuple[Tensor, Tensor]:
    """PyTorch implementation of fisheye projection for 3D Gaussians.

    Args:
        means: Gaussian means in camera coordinate system. [C, N, 3].
        covars: Gaussian covariances in camera coordinate system. [C, N, 3, 3].
        Ks: Camera intrinsics. [C, 3, 3].
        width: Image width.
        height: Image height.

    Returns:
        A tuple:

        - **means2d**: Projected means. [C, N, 2].
        - **cov2d**: Projected covariances. [C, N, 2, 2].
    """
    C, N, _ = means.shape

    x, y, z = torch.unbind(means, dim=-1)  # [C, N]

    fx = Ks[..., 0, 0, None]  # [C, 1]
    fy = Ks[..., 1, 1, None]  # [C, 1]
    cx = Ks[..., 0, 2, None]  # [C, 1]
    cy = Ks[..., 1, 2, None]  # [C, 1]

    eps = 0.0000001
    xy_len = (x**2 + y**2) ** 0.5 + eps
    theta = torch.atan2(xy_len, z + eps)
    means2d = torch.stack(
        [
            x * fx * theta / xy_len + cx,
            y * fy * theta / xy_len + cy,
        ],
        dim=-1,
    )

    x2 = x * x + eps
    y2 = y * y
    xy = x * y
    x2y2 = x2 + y2
    x2y2z2_inv = 1.0 / (x2y2 + z * z)
    b = torch.atan2(xy_len, z) / xy_len / x2y2
    a = z * x2y2z2_inv / (x2y2)
    J = torch.stack(
        [
            fx * (x2 * a + y2 * b),
            fx * xy * (a - b),
            -fx * x * x2y2z2_inv,
            fy * xy * (a - b),
            fy * (y2 * a + x2 * b),
            -fy * y * x2y2z2_inv,
        ],
        dim=-1,
    ).reshape(C, N, 2, 3)

    cov2d = torch.einsum("...ij,...jk,...kl->...il", J, covars, J.transpose(-1, -2))
    return means2d, cov2d  # [C, N, 2], [C, N, 2, 2]


def _ortho_proj(
    means: Tensor,  # [C, N, 3]
    covars: Tensor,  # [C, N, 3, 3]
    Ks: Tensor,  # [C, 3, 3]
    width: int,
    height: int,
) -> Tuple[Tensor, Tensor]:
    """PyTorch implementation of orthographic projection for 3D Gaussians.

    Args:
        means: Gaussian means in camera coordinate system. [C, N, 3].
        covars: Gaussian covariances in camera coordinate system. [C, N, 3, 3].
        Ks: Camera intrinsics. [C, 3, 3].
        width: Image width.
        height: Image height.

    Returns:
        A tuple:

        - **means2d**: Projected means. [C, N, 2].
        - **cov2d**: Projected covariances. [C, N, 2, 2].
    """
    C, N, _ = means.shape

    fx = Ks[..., 0, 0, None]  # [C, 1]
    fy = Ks[..., 1, 1, None]  # [C, 1]

    O = torch.zeros((C, 1), device=means.device, dtype=means.dtype)
    J = torch.stack([fx, O, O, O, fy, O], dim=-1).reshape(C, 1, 2, 3).repeat(1, N, 1, 1)

    cov2d = torch.einsum("...ij,...jk,...kl->...il", J, covars, J.transpose(-1, -2))
    means2d = (
        means[..., :2] * Ks[:, None, [0, 1], [0, 1]] + Ks[:, None, [0, 1], [2, 2]]
    )  # [C, N, 2]
    return means2d, cov2d  # [C, N, 2], [C, N, 2, 2]


def _world_to_cam(
    means: Tensor,  # [N, 3]
    covars: Tensor,  # [N, 3, 3]
    viewmats: Tensor,  # [C, 4, 4]
) -> Tuple[Tensor, Tensor]:
    """PyTorch implementation of world to camera transformation on Gaussians.

    Args:
        means: Gaussian means in world coordinate system. [C, N, 3].
        covars: Gaussian covariances in world coordinate system. [C, N, 3, 3].
        viewmats: world to camera transformation matrices. [C, 4, 4].

    Returns:
        A tuple:

        - **means_c**: Gaussian means in camera coordinate system. [C, N, 3].
        - **covars_c**: Gaussian covariances in camera coordinate system. [C, N, 3, 3].
    """
    R = viewmats[:, :3, :3]  # [C, 3, 3]
    t = viewmats[:, :3, 3]  # [C, 3]
    means_c = torch.einsum("cij,nj->cni", R, means) + t[:, None, :]  # (C, N, 3)
    covars_c = torch.einsum("cij,njk,clk->cnil", R, covars, R)  # [C, N, 3, 3]
    return means_c, covars_c


def compute_determinant_3d(covars3d: torch.Tensor) -> torch.Tensor:
    """
    Computes the determinant of 3D covariance matrices without using torch.det().

    Args:
        covars3d (torch.Tensor): Tensor of shape [C, N, 3, 3] representing covariance matrices.

    Returns:
        torch.Tensor: Determinants of shape [C, N].
    """
    # Extract individual elements
    a = covars3d[..., 0, 0]  # [C, N]
    b = covars3d[..., 0, 1]  # [C, N]
    c = covars3d[..., 0, 2]  # [C, N]
    d = covars3d[..., 1, 0]  # [C, N]
    e = covars3d[..., 1, 1]  # [C, N]
    f = covars3d[..., 1, 2]  # [C, N]
    g = covars3d[..., 2, 0]  # [C, N]
    h = covars3d[..., 2, 1]  # [C, N]
    i = covars3d[..., 2, 2]  # [C, N]

    # Compute the determinant using the expansion formula
    det = a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)

    return det  # S

def compute_inverse_3d(covars3d: torch.Tensor, det: torch.Tensor) -> torch.Tensor:
    """
    Computes the inverse of 3D covariance matrices without using torch.inv().

    Args:
        covars3d (torch.Tensor): Tensor of shape [C, N, 3, 3] representing covariance matrices.
        det (torch.Tensor): Determinant of each covariance matrix. Shape: [C, N]

    Returns:
        torch.Tensor: Inverse covariance matrices of shape [C, N, 3, 3].
    """
    # Extract individual elements
    a = covars3d[..., 0, 0]
    b = covars3d[..., 0, 1]
    c = covars3d[..., 0, 2]
    d = covars3d[..., 1, 1]
    e = covars3d[..., 1, 2]
    f = covars3d[..., 2, 2]

    # Compute cofactors
    Cof11 = d * f - e ** 2
    Cof12 = -(b * f - c * e)
    Cof13 = b * e - c * d
    Cof21 = -(b * f - c * e)
    Cof22 = a * f - c ** 2
    Cof23 = -(a * e - b * c)
    Cof31 = b * e - c * d
    Cof32 = -(a * e - b * c)
    Cof33 = a * d - b ** 2

    # Stack cofactors to form the adjugate matrix
    adjugate = torch.stack([
        torch.stack([Cof11, Cof21, Cof31], dim=-1),
        torch.stack([Cof12, Cof22, Cof32], dim=-1),
        torch.stack([Cof13, Cof23, Cof33], dim=-1)
    ], dim=-2)  # Shape: [C, N, 3, 3]

    # Compute inverse by dividing adjugate by determinant
    inverse = adjugate / det.unsqueeze(-1).unsqueeze(-1)  # Broadcasting det over last two dims

    return inverse  # Shape: [C, N, 3, 3]


#TODO: Need to be modified by 3d gaussian
def _fully_fused_projection(
    means: Tensor,  # [N, 3]
    covars: Tensor,  # [N, 3, 3]
    viewmats: Tensor,  # [C, 4, 4]
    Ks: Tensor,  # [C, 3, 3]
    width: int,
    height: int,
    eps2d: float = 0.3,
    near_plane: float = 0.01,
    far_plane: float = 1e10,
    calc_compensations: bool = False,
    camera_model: Literal["pinhole", "ortho", "fisheye"] = "pinhole",
) -> Tuple[Tensor, Tensor, Tensor, Tensor, Optional[Tensor]]:
    """PyTorch implementation of `gsplat.cuda._wrapper.fully_fused_projection()`
    
    Project 3D Gaussian parameters from world space into the 2D image space of one or more cameras,
    optionally computing compensation factors and conic matrices for each projected Gaussian.

    This function takes a set of 3D Gaussian distributions (each defined by a mean and covariance),
    transforms them from a common world coordinate system into one or more camera coordinate systems,
    projects them onto each camera’s 2D image plane, and computes per-Gaussian elliptical footprints
    (radii) and additional derived quantities.

    The projection process can be performed using one of three camera models:
    - **pinhole**: A standard perspective projection model.
    - **ortho**: An orthographic projection.
    - **fisheye**: A fisheye (wide-angle) projection model.

    Depending on the chosen camera model, the function computes the 2D projected means and covariances,
    as well as various depth measures, adjusted covariances, and optional compensation factors.

    Parameters
    ----------
    means : Tensor
        A tensor of shape `[N, 3]` representing the 3D mean positions of N Gaussian components 
        in the world coordinate system.
    
    covars : Tensor
        A tensor of shape `[N, 3, 3]` representing the 3D covariance matrices of the N Gaussian 
        components in world coordinates. Each covariance must be a positive semi-definite matrix.
    
    viewmats : Tensor
        A tensor of shape `[C, 4, 4]` representing the camera extrinsic matrices for C different 
        cameras. Each matrix transforms points from world coordinates into the camera’s view space.
    
    Ks : Tensor
        A tensor of shape `[C, 3, 3]` representing the intrinsic camera matrices for C cameras.
        For the pinhole camera model, this typically includes focal lengths and principal point 
        offsets. For orthographic or fisheye models, it may be adapted accordingly.
    
    width : int
        The width (in pixels) of the image or viewport associated with each camera.
    
    height : int
        The height (in pixels) of the image or viewport associated with each camera.
    
    eps2d : float, optional
        A small regularization term added to the top-left 2x2 block of the projected covariance 
        matrices. This helps maintain numerical stability and prevents singularities in the 
        2D covariance matrices. Default is 0.3.
    
    near_plane : float, optional
        The near clipping plane distance. Gaussians whose transformed depth is less than this 
        value will be considered invalid and will have zero radius. Default is 0.01.
    
    far_plane : float, optional
        The far clipping plane distance. Gaussians whose transformed depth is greater than this 
        value will be considered invalid and will have zero radius. Default is 1e10.
    
    calc_compensations : bool, optional
        If `True`, compute and return compensation factors that relate the original determinant 
        of the 3D covariance to the adjusted determinant after 2D regularization (`eps2d`) is applied. 
        These compensation values can be useful for adjusting the weight of each Gaussian after 
        projection. Default is `False`.
    
    camera_model : {"pinhole", "ortho", "fisheye"}, optional
        Specifies the camera projection model to use.
        - `"pinhole"`: Uses a standard perspective projection.
        - `"ortho"`: Uses an orthographic projection.
        - `"fisheye"`: Uses a fisheye projection model.
        
        Default is `"pinhole"`.

    Returns
    -------
    radii : Tensor
        A tensor of shape `[C, N]` representing the radius (in pixels) of an elliptical footprint 
        that bounds each projected Gaussian on the image plane. If a Gaussian is invalid (outside 
        the near/far clipping planes or outside the image boundaries), its radius will be set to 0.
    
    means2d : Tensor
        A tensor of shape `[C, N, 2]` containing the projected 2D mean positions (in pixel 
        coordinates) of the Gaussians in each camera’s image space.
    
    depths : Tensor
        A tensor of shape `[C, N]` containing the per-Gaussian depth values in camera coordinates. 
        For a pinhole camera model, this corresponds to the Z-coordinate in the camera’s view space. 
        For orthographic and fisheye models, this also corresponds to the camera-space depth.
    
    depths_persp : Tensor
        A tensor of shape `[C, N]` giving perspective-corrected depths for the pinhole camera model. 
        For orthographic or fisheye models, this value may be `None`. Perspective depth is typically 
        used for proper weighting in a rendering pipeline.
    
    conics : Tensor
        A tensor of shape `[C, N, 3, 3]` representing the inverse covariance matrices (conics) 
        of the projected Gaussians on the image plane. This can be used to evaluate Gaussian 
        densities or for further geometric processing in image space.
    
    compensations : Tensor or None
        If `calc_compensations` is `True`, returns a tensor of shape `[C, N]` representing the 
        ratio of the original Gaussian covariance determinant to the adjusted determinant after 
        adding `eps2d`. This factor can be used to compensate for the added regularization. If 
        `calc_compensations` is `False`, `None` is returned.

    Notes
    -----
    - This function leverages transformations to camera coordinates using `viewmats` and 
      projection using `Ks` based on the chosen `camera_model`.
    - The `radii` are computed as a scalar bounding radius derived from the projected 2D covariance.
      They represent a conservative estimate of the Gaussian’s footprint in image space.
    - Gaussians that project partially or fully outside the image boundaries or lie outside the 
      near/far clipping planes have their radii (and potentially other attributes) set to zero or 
      truncated values.
    - The `eps2d` parameter ensures numerical stability when dealing with very small or degenerate 
      Gaussians, by broadening their 2D footprint slightly.
    - For the pinhole model, `depths_persp` provides an additional depth measure useful for 
      perspective-correct rendering or sorting.
    - Some internal computations (such as `_world_to_cam`, `_ortho_proj`, `_fisheye_proj`, 
      `_persp_proj`, `compute_determinant_3d`, and `compute_inverse_3d`) are assumed to be 
      implemented separately, handling the lower-level math for transformations and projections.

    Example
    -------
    Suppose you have:
    - `N` Gaussian distributions defined in world space.
    - `C` cameras, each with a known view matrix `viewmats[c]` and intrinsic matrix `Ks[c]`.

    You can project them as follows:
    ```python
    radii, means2d, depths, depths_persp, conics, comps = _fully_fused_projection(
        means, covars, viewmats, Ks, width=1920, height=1080, camera_model="pinhole"
    )
    ```

    After this, you can use `means2d` and `radii` to visualize Gaussian footprints on each camera’s 
    image, or `conics` and `comps` for more advanced image-space operations.
    
    .. note::

        This is a minimal implementation of fully fused version, which has more
        arguments. Not all arguments are supported.
    """
    # transform the means to camera perspective otherwise knows as eye
    means_c, covars_c = _world_to_cam(means, covars, viewmats)

    if camera_model == "ortho":
        means2d, covars2d = _ortho_proj(means_c, covars_c, Ks, width, height) # TODO: change to allow for neg gaussian with 3d covars
    elif camera_model == "fisheye":
        means2d, covars2d = _fisheye_proj(means_c, covars_c, Ks, width, height) # TODO: change to allow for neg gaussian with 3d covars
    elif camera_model == "pinhole":
        means2d, covars3d, depths_persp = _persp_proj(means_c, covars_c, Ks, width, height)
    else:
        assert_never(camera_model)

    det_orig = compute_determinant_3d(covars3d)

    #TODO: Make sure this eye is only applied to x and y when changing to 3d gaussian. 
    covars3d[...,:2,:2] = covars3d[...,:2,:2] + torch.eye(2, device=means.device, dtype=means.dtype) * eps2d

    det = compute_determinant_3d(covars3d)
    det = det.clamp(min=1e-10)

    if calc_compensations:
        compensations = torch.sqrt(torch.clamp(det_orig / det, min=0.0))
    else:
        compensations = None

    conics = compute_inverse_3d(covars3d,det) # [C, N, 3, 3]

    depths = means_c[..., 2]  # [C, N]

    det_2d =  (
        covars3d[..., 0, 0] * covars3d[..., 1, 1]
        - covars3d[..., 0, 1] * covars3d[..., 1, 0]
    )

    b = (covars3d[..., 0, 0] + covars3d[..., 1, 1]) / 2  # (...,)
    v1 = b + torch.sqrt(torch.clamp(b**2 - det_2d, min=0.01))  # (...,)
    radius = torch.ceil(3.0 * torch.sqrt(v1))  # (...,)
    # v2 = b - torch.sqrt(torch.clamp(b**2 - det, min=0.01))  # (...,)
    # radius = torch.ceil(3.0 * torch.sqrt(torch.max(v1, v2)))  # (...,)

    valid = (det > 0) & (depths > near_plane) & (depths < far_plane)
    radius[~valid] = 0.0

    inside = (
        (means2d[..., 0] + radius > 0)
        & (means2d[..., 0] - radius < width)
        & (means2d[..., 1] + radius > 0)
        & (means2d[..., 1] - radius < height)
    )
    radius[~inside] = 0.0

    radii = radius.int()
    return radii, means2d, depths, depths_persp, conics, covars3d, compensations


@torch.no_grad()
def _isect_tiles(
    means2d: Tensor,
    radii: Tensor,
    depths: Tensor,
    tile_size: int,
    tile_width: int,
    tile_height: int,
    sort: bool = True,
) -> Tuple[Tensor, Tensor, Tensor]:
    """Pytorch implementation of `gsplat.cuda._wrapper.isect_tiles()`.

    .. note::

        This is a minimal implementation of the fully fused version, which has more
        arguments. Not all arguments are supported.
    """
    C, N = means2d.shape[:2]
    device = means2d.device

    # compute tiles_per_gauss
    tile_means2d = means2d / tile_size
    tile_radii = radii / tile_size
    tile_mins = torch.floor(tile_means2d - tile_radii[..., None]).int()
    tile_maxs = torch.ceil(tile_means2d + tile_radii[..., None]).int()
    tile_mins[..., 0] = torch.clamp(tile_mins[..., 0], 0, tile_width)
    tile_mins[..., 1] = torch.clamp(tile_mins[..., 1], 0, tile_height)
    tile_maxs[..., 0] = torch.clamp(tile_maxs[..., 0], 0, tile_width)
    tile_maxs[..., 1] = torch.clamp(tile_maxs[..., 1], 0, tile_height)
    tiles_per_gauss = (tile_maxs - tile_mins).prod(dim=-1)  # [C, N]
    tiles_per_gauss *= radii > 0.0

    n_isects = tiles_per_gauss.sum().item()
    isect_ids = torch.empty(n_isects, dtype=torch.int64, device=device)
    flatten_ids = torch.empty(n_isects, dtype=torch.int32, device=device)

    cum_tiles_per_gauss = torch.cumsum(tiles_per_gauss.flatten(), dim=0)
    tile_n_bits = (tile_width * tile_height).bit_length()

    def binary(num):
        return "".join("{:0>8b}".format(c) for c in struct.pack("!f", num))

    def kernel(cam_id, gauss_id):
        if radii[cam_id, gauss_id] <= 0.0:
            return
        index = cam_id * N + gauss_id
        curr_idx = cum_tiles_per_gauss[index - 1] if index > 0 else 0

        depth_id = struct.unpack("i", struct.pack("f", depths[cam_id, gauss_id]))[0]

        tile_min = tile_mins[cam_id, gauss_id]
        tile_max = tile_maxs[cam_id, gauss_id]
        for y in range(tile_min[1], tile_max[1]):
            for x in range(tile_min[0], tile_max[0]):
                tile_id = y * tile_width + x
                isect_ids[curr_idx] = (
                    (cam_id << 32 << tile_n_bits) | (tile_id << 32) | depth_id
                )
                flatten_ids[curr_idx] = index  # flattened index
                curr_idx += 1

    for cam_id in range(C):
        for gauss_id in range(N):
            kernel(cam_id, gauss_id)

    if sort:
        isect_ids, sort_indices = torch.sort(isect_ids)
        flatten_ids = flatten_ids[sort_indices]

    return tiles_per_gauss.int(), isect_ids, flatten_ids


@torch.no_grad()
def _isect_offset_encode(
    isect_ids: Tensor, C: int, tile_width: int, tile_height: int
) -> Tensor:
    """Pytorch implementation of `gsplat.cuda._wrapper.isect_offset_encode()`.

    .. note::

        This is a minimal implementation of the fully fused version, which has more
        arguments. Not all arguments are supported.
    """
    tile_n_bits = (tile_width * tile_height).bit_length()
    tile_counts = torch.zeros(
        (C, tile_height, tile_width), dtype=torch.int64, device=isect_ids.device
    )

    isect_ids_uq, counts = torch.unique_consecutive(isect_ids >> 32, return_counts=True)

    cam_ids_uq = isect_ids_uq >> tile_n_bits
    tile_ids_uq = isect_ids_uq & ((1 << tile_n_bits) - 1)
    tile_ids_x_uq = tile_ids_uq % tile_width
    tile_ids_y_uq = tile_ids_uq // tile_width

    tile_counts[cam_ids_uq, tile_ids_y_uq, tile_ids_x_uq] = counts

    cum_tile_counts = torch.cumsum(tile_counts.flatten(), dim=0).reshape_as(tile_counts)
    offsets = cum_tile_counts - tile_counts
    return offsets.int()


@torch.no_grad()
def sort_ids_and_get_offsets(original_ids):
    """
    Sorts the input tensor of IDs and returns the sorted tensor along with a list of offsets.
    
    Parameters:
    original_ids (torch.Tensor): 1D tensor containing unsorted IDs.
    
    Returns:
    sorted_ids (torch.Tensor): Tensor of sorted IDs.
    offsets (torch.Tensor): 1D tensor containing offsets indicating where each unique ID starts.
    unique_ids (torch.Tensor): Tensor of unique sorted IDs.
    counts (torch.Tensor): Tensor containing counts of each unique ID.
    """
    # Ensure the input is a 1D tensor
    if original_ids.dim() != 1:
        raise ValueError("Input tensor must be 1-dimensional")
    
    # Step 1: Sort the tensor
    sorted_ids, sorted_indices = torch.sort(original_ids)
    
    # Step 2: Find unique IDs and their counts
    unique_ids, counts = torch.unique(sorted_ids, return_counts=True)
    
    # Step 3: Compute offsets
    # Compute the cumulative sum of counts to get the end indices
    cumulative_counts = torch.cumsum(counts, dim=0)
    
    # Prepend 0 to the cumulative counts to get the starting offsets
    offsets = torch.cat((torch.tensor([0], device=counts.device), cumulative_counts))
    
    return sorted_ids, sorted_indices, offsets, unique_ids, counts


def delta_sparce(fci_pos, fgi_pos, fci_neg, fgi_neg, pixel_coords, means2d, depths_persp, covars3d, i):
    
    with torch.no_grad():
        P = len(fci_pos)

        N = len(fci_neg)

        depth_threshold = 1*torch.sqrt(covars3d[fci_neg,fgi_neg,2,2]) # 3 standard deviations away [Neg]

        deltas_z = depths_persp[fci_pos,fgi_pos,None] - depths_persp[None,fci_neg,fgi_neg] # [Pos, Neg]

        mask = deltas_z.abs() <= depth_threshold.unsqueeze(0)

        valid_indices = mask.nonzero(as_tuple=False)  # shape [nnz, 2], 

    p_idx = valid_indices[:,0]
    n_idx = valid_indices[:,1]

    deltas_xy_vals = pixel_coords[fgi_pos[p_idx],:] - means2d[fci_neg[n_idx], fgi_neg[n_idx],:]
    deltas_z_vals = deltas_z[p_idx,n_idx]

    deltas_z_vals = rearrange(deltas_z_vals,'M -> M 1')
    delta_vals = torch.cat((deltas_xy_vals,deltas_z_vals),dim=-1)
    
    indices_for_deltas = torch.stack([p_idx, n_idx], dim=0) 
    delta_sp = torch.sparse_coo_tensor(indices_for_deltas, delta_vals, size=(P, N, 3)).coalesce()

    return delta_sp


#TODO: function we need to change for negative gaussian splatting
# I need the depths as well

def accumulate(
    means2d: Tensor,  # [C, N, 2]
    conics: Tensor,  # [C, N, 3]
    covars3d: Tensor,  # [C, N, 3]
    opacities: Tensor,  # [C, N]
    colors: Tensor,  # [C, N, channels]
    gaussian_ids: Tensor,  # [M]
    pixel_ids: Tensor,  # [M]
    camera_ids: Tensor,  # [M]
    tile_height: int,
    tile_width: int,
    image_width: int,
    image_height: int,
    tile_size: int,
    depths_persp: Tensor, # [C, N]
) -> Tuple[Tensor, Tensor]:
    """Alpah compositing of 2D Gaussians in Pure Pytorch.

    This function performs alpha compositing for Gaussians based on the pair of indices
    {gaussian_ids, pixel_ids, camera_ids}, which annotates the intersection between all
    pixels and Gaussians. These intersections can be accquired from
    `gsplat.rasterize_to_indices_in_range`.

    .. note::

        This function exposes the alpha compositing process into pure Pytorch.
        So it relies on Pytorch's autograd for the backpropagation. It is much slower
        than our fully fused rasterization implementation and comsumes much more GPU memory.
        But it could serve as a playground for new ideas or debugging, as no backward
        implementation is needed.

    .. warning::

        This function requires the `nerfacc` package to be installed. Please install it
        using the following command `pip install nerfacc`.

    Args:
        means2d: Gaussian means in 2D. [C, N, 2]
        conics: Inverse of the 2D Gaussian covariance, Only upper triangle values. [C, N, 3]
        opacities: Per-view Gaussian opacities (for example, when antialiasing is
            enabled, Gaussian in each view would efficiently have different opacity). [C, N]
        colors: Per-view Gaussian colors. Supports N-D features. [C, N, channels]
        gaussian_ids: Collection of Gaussian indices to be rasterized. A flattened list of shape [M].
        pixel_ids: Collection of pixel indices (row-major) to be rasterized. A flattened list of shape [M].
        camera_ids: Collection of camera indices to be rasterized. A flattened list of shape [M].
        image_width: Image width.
        image_height: Image height.

    Returns:
        A tuple:

        - **renders**: Accumulated colors. [C, image_height, image_width, channels]
        - **alphas**: Accumulated opacities. [C, image_height, image_width, 1]
    """

    try:
        from nerfacc import accumulate_along_rays, render_weight_from_alpha
    except ImportError:
        raise ImportError("Please install nerfacc package: pip install nerfacc")

    os.environ['CUDA_LAUNCH_BLOCKING'] = '1'

    C, N = means2d.shape[:2]
    channels = colors.shape[-1]

    pixel_ids_x = pixel_ids % image_width
    pixel_ids_y = pixel_ids // image_width
    tile_ids_x = pixel_ids_x // tile_size
    tile_ids_y = pixel_ids_y // tile_size


    pixel_coords = torch.stack([pixel_ids_x, pixel_ids_y], dim=-1) + 0.5  # [M, 2] 13132MiB


    # Positive Gaussians -------------------------

    # While the oppasities may be different from each camera due to antialiasing
    # The oppacity will never flip sign due to it.
    # Determine which Gaussians are positive

    # Try to do minimal advanced indexing: first only create pos_ids as boolean mask
    pos_ids = (opacities[0, gaussian_ids].flatten() > 0)

    # Convert boolean mask to integer indices
    pos_indices = torch.nonzero(pos_ids, as_tuple=False).squeeze(-1)
    # pos_indices now contains the indices of gaussian_ids/camera_ids that are positive

    # Use integer indices to filter arrays - this typically uses less memory
    fgi_pos = gaussian_ids[pos_indices] # filtered_gaussian_ids_pos
    fci_pos  = camera_ids[pos_indices]  # filtered_camera_ids_pos

    deltas = pixel_coords[fgi_pos,:] - means2d[fci_pos,fgi_pos]  # [P, 2] 

    c = conics[fci_pos, fgi_pos,:,:]  # [P, 3, 3]

    sigmas = (
        0.5 * (c[..., 0, 0] * deltas[:, 0] ** 2 + c[..., 1, 1] * deltas[:, 1] ** 2)
        + c[:, 0, 1] * deltas[:, 0] * deltas[:, 1]
    )  # [P] 28430MiB

    alphas = torch.clamp_max(
        opacities[fci_pos, fgi_pos] * torch.exp(-sigmas), 0.999
    )  # 29962MiB

    del pixel_ids_x, pixel_ids_y, sigmas, deltas, c
    gc.collect()
    torch.cuda.empty_cache()
    
    # Negative Gaussians ------------------------

    # Try to do minimal advanced indexing: first only create neg_ids as boolean mask
    neg_ids = opacities[0,gaussian_ids].flatten()<0 # [Ne]

    neg_length = neg_ids.sum()

    if neg_length>0:

        tile_id_per_elem = camera_ids * (tile_height * tile_width) + (tile_ids_y * tile_width) + tile_ids_x

        sorted_ids, sorted_indices, offsets, unique_ids, counts = sort_ids_and_get_offsets(tile_id_per_elem)

        for i in range(0,len(offsets)-1):

            tile_indices = sorted_indices[offsets[i]:offsets[i+1]]

            # Try to do minimal advanced indexing: first only create pos_ids as boolean mask
            pos_tile_mask = (opacities[0, gaussian_ids[tile_indices]].flatten() > 0)

            # Convert boolean mask to integer indices
            pos_tile_indices = tile_indices[pos_tile_mask]

            # Try to do minimal advanced indexing: first only create pos_ids as boolean mask
            neg_tile_mask = (opacities[0, gaussian_ids[tile_indices]].flatten() < 0)

            if neg_tile_mask.sum() == 0:
                continue

            # Convert boolean mask to integer indices
            neg_tile_indices = tile_indices[neg_tile_mask]

            P = len(pos_tile_indices)
            N = len(neg_tile_indices)

            # Use integer indices to filter arrays - this typically uses less memory
            fgi_tile_pos = gaussian_ids[pos_tile_indices] # filtered_gaussian_ids_neg
            fci_tile_pos = camera_ids[pos_tile_indices]  # filtered_camera_ids_pos

            fgi_tile_neg = gaussian_ids[neg_tile_indices] # filtered_gaussian_ids_neg
            fci_tile_neg = camera_ids[neg_tile_indices]  # filtered_camera_ids_pos


            delta_sp = delta_sparce(fci_tile_pos, fgi_tile_pos, fci_tile_neg, fgi_tile_neg, pixel_coords, means2d, depths_persp, covars3d,i) # 3450 MB

            # # deltas_xy = pixel_coords[fgi_tile_pos,None,:] - means2d[None,fci_tile_neg, fgi_tile_neg]  # [Pos_chunk, Ne, 2]
            # # deltas_z = depths_persp[fci_tile_pos,fgi_tile_pos,None] - depths_persp[None,fci_tile_neg,fgi_tile_neg] # [Pos_chunk, Ne]

            # deltas_z = rearrange(deltas_z,'p n  -> p n 1')

            # deltas = torch.cat((deltas_xy,deltas_z),dim=-1)

            # Compute D in sparse form
            delta_sp_vals = delta_sp._values()    # [nnz, 3] on cuda:2
            delta_sp_idx = delta_sp._indices()     # [3, nnz], on cuda:2
            p_idx_sp = delta_sp_idx[0]
            n_idx_sp = delta_sp_idx[1]

            c = conics[fci_tile_neg[n_idx_sp], fgi_tile_neg[n_idx_sp],:,:]  # [Neg, 3, 3]

            sigmas_neg = torch.einsum('bi,bij,bj->b', delta_sp_vals, c, delta_sp_vals)  # [nnz]

            opacities_subset = opacities[fci_tile_neg[n_idx_sp], fgi_tile_neg[n_idx_sp]]
            neg_alphas_vals_sp = opacities_subset * torch.exp(-0.5 * sigmas_neg)

            # neg_alpha_sp = torch.sparse_coo_tensor(delta_sp_idx, neg_alphas_vals, size=(P, N), device=device).coalesce()


            neg_alphas = torch.zeros((P), device=neg_alphas_vals_sp.device)

            # Scatter-add the A_neg_vals into A_dense_from_sparse using the (k_idx_sp, p_idx_sp) indices.
            neg_alphas.scatter_add_(0, p_idx_sp, neg_alphas_vals_sp)

            alphas[fgi_tile_pos] = alphas[fgi_tile_pos] +  neg_alphas

            alphas[fgi_tile_pos] = alphas[fgi_tile_pos].clip(min=0)

            del neg_alphas_vals_sp, sigmas_neg, fgi_tile_pos, fci_tile_pos, fgi_tile_neg, fci_tile_neg, opacities_subset, neg_alphas, delta_sp
            gc.collect()
            torch.cuda.empty_cache()


    indices = camera_ids * image_height * image_width + pixel_ids # 38378MiB 
    total_pixels = C * image_height * image_width

    weights, trans = render_weight_from_alpha(
        alphas, ray_indices=indices, n_rays=total_pixels
    )
    renders = accumulate_along_rays(
        weights,
        colors[camera_ids, gaussian_ids],
        ray_indices=indices,
        n_rays=total_pixels,
    ).reshape(C, image_height, image_width, channels) # 42966MiB
    alphas = accumulate_along_rays(
        weights, None, ray_indices=indices, n_rays=total_pixels
    ).reshape(C, image_height, image_width, 1) # 42966MiB

    return renders, alphas

# TODO Need to pass depth
def _rasterize_to_pixels(
    means2d: Tensor,  # [C, N, 2]
    conics: Tensor,  # [C, N, 3]
    covars3d: Tensor, # [C, N, 3]
    colors: Tensor,  # [C, N, channels]
    opacities: Tensor,  # [C, N]
    image_width: int,
    image_height: int,
    tile_size: int,
    isect_offsets: Tensor,  # [C, tile_height, tile_width]
    flatten_ids: Tensor,  # [n_isects]
    depths_persp: Tensor,
    backgrounds: Optional[Tensor] = None,  # [C, channels]
    batch_per_iter: int = 100,
):
    """Pytorch implementation of `gsplat.cuda._wrapper.rasterize_to_pixels()`.

    This function rasterizes 2D Gaussians to pixels in a Pytorch-friendly way. It
    iteratively accumulates the renderings within each batch of Gaussians. The
    interations are controlled by `batch_per_iter`.

    .. note::
        This is a minimal implementation of the fully fused version, which has more
        arguments. Not all arguments are supported.

    .. note::

        This function relies on Pytorch's autograd for the backpropagation. It is much slower
        than our fully fused rasterization implementation and comsumes much more GPU memory.
        But it could serve as a playground for new ideas or debugging, as no backward
        implementation is needed.

    .. warning::

        This function requires the `nerfacc` package to be installed. Please install it
        using the following command `pip install nerfacc`.
    """
    from .cuda._wrapper import rasterize_to_indices_in_range

    tile_height, tile_width = isect_offsets.shape[1], isect_offsets.shape[2]
    C, N = means2d.shape[:2]
    n_isects = len(flatten_ids)
    device = means2d.device

    render_colors = torch.zeros(
        (C, image_height, image_width, colors.shape[-1]), device=device
    )
    render_alphas = torch.zeros((C, image_height, image_width, 1), device=device)

    # Split Gaussians into batches and iteratively accumulate the renderings
    block_size = tile_size * tile_size
    isect_offsets_fl = torch.cat(
        [isect_offsets.flatten(), torch.tensor([n_isects], device=device)]
    )
    max_range = (isect_offsets_fl[1:] - isect_offsets_fl[:-1]).max().item()
    num_batches = (max_range + block_size - 1) // block_size
    for step in range(0, num_batches, batch_per_iter):
        transmittances = 1.0 - render_alphas[..., 0]

        # Find the M intersections between pixels and gaussians.
        # Each intersection corresponds to a tuple (gs_id, pixel_id, camera_id)
        conic_0 =conics[..., 0, 0]  # Shape: [C, N]
        conic_1 = conics[..., 0, 1]  # Shape: [C, N]
        conic_2 = conics[..., 1, 1]  # Shape: [C, N]

        conics_2d = torch.stack(
            [conic_0, conic_1, conic_2],
            dim=-1) 
        # Increased the GPU men ------
        gs_ids, pixel_ids, camera_ids = rasterize_to_indices_in_range(
            step,
            step + batch_per_iter,
            transmittances,
            means2d,
            conics_2d,
            torch.abs(opacities),
            image_width,
            image_height,
            tile_size,
            isect_offsets,
            flatten_ids,
            depths_persp
        )  # [M], [M]

        if len(gs_ids) == 0:
            break

        # TODO Need to pass depth
        # Accumulate the renderings within this batch of Gaussians.
        # Almost fulled gpu usage  -------- 
        renders_step, accs_step = accumulate(
            means2d,
            conics,
            covars3d,
            opacities,
            colors,
            gs_ids,
            pixel_ids,
            camera_ids,
            tile_height,
            tile_width,
            image_width,
            image_height,
            tile_size,
            depths_persp
        ) # 7012MiB
        render_colors = render_colors + renders_step * transmittances[..., None]
        render_alphas = render_alphas + accs_step * transmittances[..., None]

    render_alphas = render_alphas
    if backgrounds is not None:
        render_colors = render_colors + backgrounds[:, None, None, :] * (
            1.0 - render_alphas
        )

    return render_colors, render_alphas


def _eval_sh_bases_fast(basis_dim: int, dirs: Tensor):
    """
    Evaluate spherical harmonics bases at unit direction for high orders
    using approach described by
    Efficient Spherical Harmonic Evaluation, Peter-Pike Sloan, JCGT 2013
    https://jcgt.org/published/0002/02/06/


    :param basis_dim: int SH basis dim. Currently, only 1-25 square numbers supported
    :param dirs: torch.Tensor (..., 3) unit directions

    :return: torch.Tensor (..., basis_dim)

    See reference C++ code in https://jcgt.org/published/0002/02/06/code.zip
    """
    result = torch.empty(
        (*dirs.shape[:-1], basis_dim), dtype=dirs.dtype, device=dirs.device
    )

    result[..., 0] = 0.2820947917738781

    if basis_dim <= 1:
        return result

    x, y, z = dirs.unbind(-1)

    fTmpA = -0.48860251190292
    result[..., 2] = -fTmpA * z
    result[..., 3] = fTmpA * x
    result[..., 1] = fTmpA * y

    if basis_dim <= 4:
        return result

    z2 = z * z
    fTmpB = -1.092548430592079 * z
    fTmpA = 0.5462742152960395
    fC1 = x * x - y * y
    fS1 = 2 * x * y
    result[..., 6] = 0.9461746957575601 * z2 - 0.3153915652525201
    result[..., 7] = fTmpB * x
    result[..., 5] = fTmpB * y
    result[..., 8] = fTmpA * fC1
    result[..., 4] = fTmpA * fS1

    if basis_dim <= 9:
        return result

    fTmpC = -2.285228997322329 * z2 + 0.4570457994644658
    fTmpB = 1.445305721320277 * z
    fTmpA = -0.5900435899266435
    fC2 = x * fC1 - y * fS1
    fS2 = x * fS1 + y * fC1
    result[..., 12] = z * (1.865881662950577 * z2 - 1.119528997770346)
    result[..., 13] = fTmpC * x
    result[..., 11] = fTmpC * y
    result[..., 14] = fTmpB * fC1
    result[..., 10] = fTmpB * fS1
    result[..., 15] = fTmpA * fC2
    result[..., 9] = fTmpA * fS2

    if basis_dim <= 16:
        return result

    fTmpD = z * (-4.683325804901025 * z2 + 2.007139630671868)
    fTmpC = 3.31161143515146 * z2 - 0.47308734787878
    fTmpB = -1.770130769779931 * z
    fTmpA = 0.6258357354491763
    fC3 = x * fC2 - y * fS2
    fS3 = x * fS2 + y * fC2
    result[..., 20] = 1.984313483298443 * z2 * (
        1.865881662950577 * z2 - 1.119528997770346
    ) + -1.006230589874905 * (0.9461746957575601 * z2 - 0.3153915652525201)
    result[..., 21] = fTmpD * x
    result[..., 19] = fTmpD * y
    result[..., 22] = fTmpC * fC1
    result[..., 18] = fTmpC * fS1
    result[..., 23] = fTmpB * fC2
    result[..., 17] = fTmpB * fS2
    result[..., 24] = fTmpA * fC3
    result[..., 16] = fTmpA * fS3
    return result


def _spherical_harmonics(
    degree: int,
    dirs: torch.Tensor,  # [..., 3]
    coeffs: torch.Tensor,  # [..., K, 3]
):
    """Pytorch implementation of `gsplat.cuda._wrapper.spherical_harmonics()`."""
    dirs = F.normalize(dirs, p=2, dim=-1)
    num_bases = (degree + 1) ** 2
    bases = torch.zeros_like(coeffs[..., 0])
    bases[..., :num_bases] = _eval_sh_bases_fast(num_bases, dirs)
    return (bases[..., None] * coeffs).sum(dim=-2)
