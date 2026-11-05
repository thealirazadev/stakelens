// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @notice Single-token staking with rewards streamed pro rata at a fixed rate per second.
/// @dev Rewards are paid from tokens held above `totalStaked`, funded by plain transfers.
///      Principal is never used to pay rewards, so `unstake` works even if rewards run out.
///      There is no owner and no admin function.
contract StakingVault is ReentrancyGuard {
    using SafeERC20 for IERC20;

    uint256 private constant PRECISION = 1e18;

    IERC20 public immutable token;
    uint256 public immutable rewardRate;

    uint256 public totalStaked;
    mapping(address user => uint256 amount) public stakedOf;

    uint256 private accRewardPerToken;
    uint256 private lastUpdate;
    mapping(address user => uint256 value) private userAccPaid;
    mapping(address user => uint256 value) private accrued;

    event Staked(address indexed user, uint256 amount);
    event Unstaked(address indexed user, uint256 amount);
    event RewardClaimed(address indexed user, uint256 amount);

    error ZeroAmount();
    error InsufficientStake(uint256 requested, uint256 available);
    error RewardsUnderfunded(uint256 owed, uint256 available);

    constructor(IERC20 token_, uint256 rewardRate_) {
        token = token_;
        rewardRate = rewardRate_;
        lastUpdate = block.timestamp;
    }

    modifier updateReward(address user) {
        accRewardPerToken = _currentAcc();
        lastUpdate = block.timestamp;
        accrued[user] = pendingReward(user);
        userAccPaid[user] = accRewardPerToken;
        _;
    }

    function stake(uint256 amount) external nonReentrant updateReward(msg.sender) {
        if (amount == 0) revert ZeroAmount();
        totalStaked += amount;
        stakedOf[msg.sender] += amount;
        token.safeTransferFrom(msg.sender, address(this), amount);
        emit Staked(msg.sender, amount);
    }

    function unstake(uint256 amount) external nonReentrant updateReward(msg.sender) {
        if (amount == 0) revert ZeroAmount();
        uint256 available = stakedOf[msg.sender];
        if (amount > available) revert InsufficientStake(amount, available);
        stakedOf[msg.sender] = available - amount;
        totalStaked -= amount;
        token.safeTransfer(msg.sender, amount);
        emit Unstaked(msg.sender, amount);
    }

    function claim() external nonReentrant updateReward(msg.sender) {
        uint256 owed = accrued[msg.sender];
        if (owed == 0) revert ZeroAmount();
        uint256 available = rewardPool();
        if (owed > available) revert RewardsUnderfunded(owed, available);
        accrued[msg.sender] = 0;
        token.safeTransfer(msg.sender, owed);
        emit RewardClaimed(msg.sender, owed);
    }

    function pendingReward(address user) public view returns (uint256) {
        uint256 delta = _currentAcc() - userAccPaid[user];
        return accrued[user] + (stakedOf[user] * delta) / PRECISION;
    }

    function rewardPool() public view returns (uint256) {
        return token.balanceOf(address(this)) - totalStaked;
    }

    function _currentAcc() private view returns (uint256) {
        if (totalStaked == 0) return accRewardPerToken;
        return accRewardPerToken + ((block.timestamp - lastUpdate) * rewardRate * PRECISION) / totalStaked;
    }
}
