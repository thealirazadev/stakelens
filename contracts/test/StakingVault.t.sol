// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {Test} from "forge-std/Test.sol";
import {StakeToken} from "../src/StakeToken.sol";
import {StakingVault} from "../src/StakingVault.sol";

contract StakingVaultTest is Test {
    uint256 private constant RATE = 1e18;
    uint256 private constant POOL = 1_000_000e18;
    uint256 private constant MAX_STAKE = 1_000_000e18;

    StakeToken private token;
    StakingVault private vault;
    address private alice = makeAddr("alice");
    address private bob = makeAddr("bob");

    function setUp() public {
        token = new StakeToken(10_000_000e18);
        vault = new StakingVault(token, RATE);
        token.transfer(address(vault), POOL);
        _fund(alice);
        _fund(bob);
    }

    function _fund(address user) private {
        token.transfer(user, MAX_STAKE);
        vm.prank(user);
        token.approve(address(vault), type(uint256).max);
    }

    function test_StakeEmitsEventAndMovesTokens() public {
        vm.expectEmit(true, false, false, true, address(vault));
        emit StakingVault.Staked(alice, 100e18);
        vm.prank(alice);
        vault.stake(100e18);

        assertEq(vault.stakedOf(alice), 100e18);
        assertEq(vault.totalStaked(), 100e18);
        assertEq(token.balanceOf(address(vault)), POOL + 100e18);
    }

    function test_ZeroAmountsRevert() public {
        vm.startPrank(alice);
        vm.expectRevert(StakingVault.ZeroAmount.selector);
        vault.stake(0);
        vm.expectRevert(StakingVault.ZeroAmount.selector);
        vault.unstake(0);
        vm.expectRevert(StakingVault.ZeroAmount.selector);
        vault.claim();
        vm.stopPrank();
    }

    function test_CannotUnstakeMoreThanStaked() public {
        vm.startPrank(alice);
        vault.stake(10e18);
        vm.expectRevert(abi.encodeWithSelector(StakingVault.InsufficientStake.selector, 11e18, 10e18));
        vault.unstake(11e18);
        vm.stopPrank();
    }

    function test_RewardsSplitProRata() public {
        vm.prank(alice);
        vault.stake(300e18);
        vm.prank(bob);
        vault.stake(100e18);

        skip(100);

        assertApproxEqAbs(vault.pendingReward(alice), 75e18, 1e3);
        assertApproxEqAbs(vault.pendingReward(bob), 25e18, 1e3);
    }

    function test_NoRewardsAccrueWhileVaultIsEmpty() public {
        skip(1000);
        vm.prank(alice);
        vault.stake(100e18);
        assertEq(vault.pendingReward(alice), 0);
    }

    function test_ClaimPaysAndResets() public {
        vm.startPrank(alice);
        vault.stake(100e18);
        skip(10);
        uint256 before = token.balanceOf(alice);
        vault.claim();
        vm.stopPrank();

        assertApproxEqAbs(token.balanceOf(alice) - before, 10e18, 1e3);
        assertEq(vault.pendingReward(alice), 0);
    }

    function test_UnstakeWorksWhenRewardsRunOut() public {
        StakingVault unfunded = new StakingVault(token, RATE);
        vm.startPrank(alice);
        token.approve(address(unfunded), type(uint256).max);
        unfunded.stake(100e18);
        skip(10);

        vm.expectPartialRevert(StakingVault.RewardsUnderfunded.selector);
        unfunded.claim();

        unfunded.unstake(100e18);
        vm.stopPrank();
        assertEq(unfunded.totalStaked(), 0);
    }

    function testFuzz_StakeUnstakeReturnsPrincipal(uint256 stakeAmount, uint256 unstakeAmount) public {
        stakeAmount = bound(stakeAmount, 1, MAX_STAKE);
        unstakeAmount = bound(unstakeAmount, 1, stakeAmount);
        uint256 before = token.balanceOf(alice);

        vm.startPrank(alice);
        vault.stake(stakeAmount);
        vault.unstake(unstakeAmount);
        vm.stopPrank();

        assertEq(vault.stakedOf(alice), stakeAmount - unstakeAmount);
        assertEq(token.balanceOf(alice), before - stakeAmount + unstakeAmount);
    }

    function testFuzz_RewardsNeverExceedEmission(uint96 aliceStake, uint96 bobStake, uint32 elapsed) public {
        uint256 a = bound(uint256(aliceStake), 1e12, MAX_STAKE);
        uint256 b = bound(uint256(bobStake), 1e12, MAX_STAKE);
        vm.prank(alice);
        vault.stake(a);
        vm.prank(bob);
        vault.stake(b);

        skip(elapsed);

        assertLe(vault.pendingReward(alice) + vault.pendingReward(bob), uint256(elapsed) * RATE);
    }
}

/// @dev Drives the vault with random actions from a small set of stakers.
contract VaultHandler is Test {
    StakeToken private immutable token;
    StakingVault private immutable vault;
    address[3] private actors = [makeAddr("a"), makeAddr("b"), makeAddr("c")];

    constructor(StakeToken token_, StakingVault vault_) {
        token = token_;
        vault = vault_;
        for (uint256 i; i < actors.length; ++i) {
            vm.prank(actors[i]);
            token.approve(address(vault), type(uint256).max);
        }
    }

    function fundActors(uint256 each) external {
        for (uint256 i; i < actors.length; ++i) {
            token.transferFrom(msg.sender, actors[i], each);
        }
    }

    function stake(uint256 seed, uint256 amount) external {
        address actor = actors[seed % actors.length];
        amount = bound(amount, 0, token.balanceOf(actor));
        if (amount == 0) return;
        vm.prank(actor);
        vault.stake(amount);
    }

    function unstake(uint256 seed, uint256 amount) external {
        address actor = actors[seed % actors.length];
        amount = bound(amount, 0, vault.stakedOf(actor));
        if (amount == 0) return;
        vm.prank(actor);
        vault.unstake(amount);
    }

    function warp(uint32 seconds_) external {
        skip(bound(uint256(seconds_), 0, 1 days));
    }

    function stakedSum() external view returns (uint256 sum) {
        for (uint256 i; i < actors.length; ++i) {
            sum += vault.stakedOf(actors[i]);
        }
    }
}

contract StakingVaultInvariantTest is Test {
    StakeToken private token;
    StakingVault private vault;
    VaultHandler private handler;

    function setUp() public {
        token = new StakeToken(10_000_000e18);
        vault = new StakingVault(token, 1e18);
        handler = new VaultHandler(token, vault);
        token.approve(address(handler), type(uint256).max);
        handler.fundActors(1_000_000e18);
        targetContract(address(handler));

        bytes4[] memory actions = new bytes4[](3);
        actions[0] = VaultHandler.stake.selector;
        actions[1] = VaultHandler.unstake.selector;
        actions[2] = VaultHandler.warp.selector;
        targetSelector(FuzzSelector({addr: address(handler), selectors: actions}));
    }

    function invariant_PrincipalIsAlwaysBacked() public view {
        assertGe(token.balanceOf(address(vault)), vault.totalStaked());
    }

    function invariant_TotalStakedMatchesUserBalances() public view {
        assertEq(vault.totalStaked(), handler.stakedSum());
    }
}
