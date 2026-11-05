// SPDX-License-Identifier: MIT
pragma solidity ^0.8.28;

import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

/// @notice Fixed-supply ERC-20 used for staking and rewards. The whole supply goes to the deployer.
contract StakeToken is ERC20 {
    constructor(uint256 initialSupply) ERC20("Stake Token", "STK") {
        _mint(msg.sender, initialSupply);
    }
}
